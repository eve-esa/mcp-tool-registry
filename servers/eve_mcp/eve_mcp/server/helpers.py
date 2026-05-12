"""Shared helper functions for the EVE MCP server."""

import json
import logging
import os
import re

from eve_api import EVEClient

from .constants import (
    DEFAULT_BASE_URL,
    DEFAULT_COLLECTION,
    DEFAULT_K,
    DEFAULT_STREAM_TIMEOUT,
)
from .enums import EventType

logger = logging.getLogger(__name__)

JSON_FENCE_MARKER = "```json"


class NoTagFoundError(Exception):
    """Exception to raise when no tag is found."""


def extract_xml_tag(text, tag):
    """Extract xml tag from text."""
    p1 = text.find(f"<{tag}>")
    p2 = text.find(f"</{tag}>")
    if p1 < 0 or p2 <= p1:
        raise NoTagFoundError(f"no {tag} found in genai response")

    return text[p1 + len(tag) + 2 : p2]


def extract_tag(text, tag):
    """Extract the first ```{tag} ... ``` markdown fence from text.

    Uses a non-greedy match so that subsequent fences in the same text
    are not consumed.
    """
    pattern = rf"```{tag}(.*?)```"
    if match := re.search(pattern, text, flags=re.DOTALL):
        return match.group(1)

    raise NoTagFoundError(f"no {tag} found in genai response")


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------
_eve_client: EVEClient | None = None


async def get_eve_client() -> EVEClient:
    """Return (and lazily initialise) the module-level EVEClient.

    On the first call the client is created, the HTTP transport is
    opened, and login is performed using ``EVE_EMAIL`` /
    ``EVE_PASSWORD`` from the environment.  Subsequent calls return
    the cached instance.

    Returns:
        An authenticated :class:`EVEClient`.

    Raises:
        RuntimeError: If ``EVE_EMAIL`` or ``EVE_PASSWORD`` are unset.
        eve_api.AuthenticationError: If login fails.
    """
    global _eve_client  # pylint: disable=global-statement

    if _eve_client is not None and _eve_client.is_authenticated():
        return _eve_client

    email = os.getenv("EVE_EMAIL")
    password = os.getenv("EVE_PASSWORD")
    base_url = os.getenv("EVE_BASE_URL", DEFAULT_BASE_URL)

    if not email or not password:
        raise RuntimeError(
            "EVE_EMAIL and EVE_PASSWORD must be set in the "
            "environment to use EVE tools."
        )

    logger.info("Initialising EVE client (base_url=%s)", base_url)
    client = EVEClient(base_url=base_url)
    await client._ensure_http_client()  # pylint: disable=protected-access
    await client.login(email, password)
    logger.info("EVE client authenticated as %s", email)

    _eve_client = client
    return _eve_client


async def ensure_logged_in() -> EVEClient:
    """Return an authenticated EVEClient, refreshing if needed.

    Wraps :func:`get_eve_client` and additionally calls
    ``auth.ensure_authenticated()`` to refresh the token when it
    is close to expiry.
    """
    client = await get_eve_client()
    await client.auth.ensure_authenticated()
    return client


async def _query_eve(
    question: str,
    collections: str = DEFAULT_COLLECTION,
    k: int = DEFAULT_K,
) -> str:
    """Query the EVE RAG system and return the assembled answer.

    Creates a temporary conversation, streams the full response,
    then deletes the conversation to avoid state accumulation.

    Args:
        question: The question to ask EVE.
        collections: Comma-separated collection names to search
            (default: ``eve-public``).
        k: Number of retrieval chunks (default: 5).

    Returns:
        JSON string with keys ``answer``, ``sources``, and
        ``conversation_id``.
    """
    client = await ensure_logged_in()

    # Create a temporary conversation
    collection_list = [c.strip() for c in collections.split(",") if c.strip()]
    logger.info(
        "query_eve: question=%r, collections=%s, k=%d",
        question[:80],
        collection_list,
        k,
    )

    conv = await client.post(
        "/conversations",
        json={"name": "mcp-query", "collections": collection_list},
    )
    conv_id: str = conv["id"]
    logger.info("Created conversation %s", conv_id)

    answer_tokens: list[str] = []
    sources: list[dict] = []
    error_msg: str | None = None

    try:  # pylint: disable=too-many-try-statements
        async for event in client.stream(
            f"/conversations/{conv_id}/stream_messages",
            json={
                "query": question,
                "public_collections": collection_list,
                "k": k,
            },
            timeout=DEFAULT_STREAM_TIMEOUT,
        ):
            event_type = event.get("type", "")

            if event_type == EventType.TOKEN.value:
                answer_tokens.append(event.get("content", ""))

            elif event_type == EventType.SOURCE.value:
                sources.append(event.get("data", event))

            elif event_type == EventType.ERROR.value:
                error_msg = event.get("message", str(event))
                logger.error("EVE stream error: %s", error_msg)
                break

            elif event_type == EventType.FINAL.value:
                # Some responses include the full text in the final
                # event; prefer the streamed tokens if we have them.
                if not answer_tokens and event.get("content"):
                    answer_tokens.append(event["content"])
                break

    finally:
        # Clean up the temporary conversation
        try:  # pylint: disable=too-many-try-statements
            await client.delete(f"/conversations/{conv_id}")
            logger.info("Deleted conversation %s", conv_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Failed to delete conversation %s: %s",
                conv_id,
                exc,
            )

    result = {
        "answer": "".join(answer_tokens),
        "sources": sources,
        "conversation_id": conv_id,
    }

    if error_msg:
        result["error"] = error_msg

    return json.dumps(result, ensure_ascii=False)


async def _extract_factuality_issues(question: str, python_code: str) -> str:
    prompt = f"""
    You are a helpful assistant for Earth Observation data analysis with Google Earth Engine.
    The Python code below was devised to answer the following question

    <QUESTION>
    {question}
    </QUESTION>

    Your task is to analyze the Google Earth Engine Python code below and extract aspects
    or issues that are making scientific or data assumptions, either explicitly or implicitly,
    and that might require factual verification.

    <PYTHON_CODE>
    {python_code}
    </PYTHON_CODE>

    Wrap your response in a ```json fenced code block containing a JSON array. Each
    array element describes one aspect, with these fields:

    ```json
    [
        {{
            "title": "A short title describing the aspect or assumption",
            "description": "Why this aspect might require factual verification",
            "facts": "Data, information, constants or facts to be verified",
            "question_for_expert": "The question to pose to an expert"
        }}
    ]
    ```
    """
    payload = json.loads(await _query_eve(prompt))
    payload["issues"] = extract_tag(payload["answer"], "json")
    return json.dumps(payload)


async def _assess_factuality_issue(  # pylint: disable=too-many-positional-arguments
    question: str,
    python_code: str,
    issue_title: str,
    issue_description: str,
    issue_facts: str,
    issue_question_for_expert: str,
) -> str:
    prompt = f"""
    I am trying to solve the following Earth Observation question

    <EO_QUESTION>
    {question}
    </EO_QUESTION>

    using the following Google Earth Engine Python script

    <PYTHON>
    {python_code}
    </PYTHON>

    I want to check the following issue raised by an Earth Observation expert

    <ISSUE_TITLE>
    {issue_title}
    </ISSUE_TITLE>

    <ISSUE_DESCRIPTION>
    {issue_description}
    </ISSUE_DESCRIPTION>

    <ISSUE_FACTS>
    {issue_facts}
    </ISSUE_FACTS>

    Your task, as an Earth Observation expert, is to answer the following question

    {issue_question_for_expert}

    Express your assessment as free Markdown text.

    If you have recommendations to fix or update the code, add a JSON object
    inside an xml tag <CODE_RECOMMENDATIONS> with the following structure
    (use double-quoted JSON, not single quotes):

    <CODE_RECOMMENDATIONS>
    {{
        "recommendation_1_title": {{
            "explanation": "an explanation of the recommendation",
            "code_snippet": "a string with python code with the suggested update"
        }},
        "recommendation_2_title": {{
            "explanation": "an explanation of the recommendation",
            "code_snippet": "a string with python code with the suggested update"
        }}
    }}
    </CODE_RECOMMENDATIONS>

    If you have no recommendations, omit the <CODE_RECOMMENDATIONS> tag entirely.
    """

    payload = json.loads(await _query_eve(prompt))
    answer = payload["answer"]

    code_recommendations = ""
    try:
        inner = extract_xml_tag(answer, "CODE_RECOMMENDATIONS")
    except NoTagFoundError:
        # Recommendations are optional per the prompt.
        pass
    else:
        block = f"<CODE_RECOMMENDATIONS>{inner}</CODE_RECOMMENDATIONS>"
        answer = answer.replace(block, "")
        code_recommendations = inner
        if JSON_FENCE_MARKER in code_recommendations:
            try:
                code_recommendations = extract_tag(
                    code_recommendations, "json"
                )
            except NoTagFoundError:
                # Leave as-is if the inner fence is malformed.
                pass

    payload["answer"] = answer
    payload["code_recommendations"] = code_recommendations
    return json.dumps(payload)
