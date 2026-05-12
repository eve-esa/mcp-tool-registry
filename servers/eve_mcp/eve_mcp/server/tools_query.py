"""EVE RAG query tool."""

import logging

from .app import mcp
from .constants import DEFAULT_COLLECTION, DEFAULT_K
from .helpers import (
    _assess_factuality_issue,
    _extract_factuality_issues,
    _query_eve,
)

logger = logging.getLogger(__name__)


@mcp.tool()
async def query_eve(
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
    return await _query_eve(question=question, collections=collections, k=k)


@mcp.tool(
    # description=(
    #     "Analyze a Google Earth Engine Python script and extract what aspects or issues "
    #     "are making scientific or data assumptions either explicitly or implicitly and might"
    #     "require factual verification. The extract aspects or issues would be presented to"
    #     "an Earth Observation expert for verification to increase the trustworthiness of the"
    #     "GEE Python code and its results."
    #     ""
    #     "The response is a a list of json structures, each one describing a specific aspect"
    #     "identified  and containing the following fields: 'title', 'description', 'facts', "
    #     "and 'question_for_expert'."
    #     ""
    # )
)
async def extract_factuality_issues(question: str, python_code: str) -> str:
    """Analyze a Google Earth Engine Python script and extract what aspects or issues
    are making scientific or data assumptions either explicitly or implicitly and might
    require factual verification. The extract aspects or issues would be presented to
    an Earth Observation expert for verification to increase the trustworthiness of the
    GEE Python code and its results.

    The response is a a list of json structures, each one describing a specific aspect
    identified  and containing the following fields: 'title', 'description', 'facts',
    and 'question_for_expert'."""
    r = await _extract_factuality_issues(
        question=question, python_code=python_code
    )
    return r


@mcp.tool(
    # description=(
    #     "makes an assessment of a factuality issue identified in a Google Earth Engine Python"
    #     "that attemps to answer an Earth Observation Question."
    #     ""
    #     "The answer includes a textual assessment, as well as possibly suggestions to update"
    #     "the Google Earth Engine Python code to improve the reliability of the answer"
    # )
)
async def assess_factuality_issue(  # pylint:disable=too-many-positional-arguments
    question: str,
    python_code: str,
    issue_title: str,
    issue_description: str,
    issue_facts: str,
    issue_question_for_expert: str,
) -> str:
    """Makes an assessment of a factuality issue identified in a Google Earth Engine Python
    that attemps to answer an Earth Observation Question.

    The answer includes a textual assessment, as well as possibly suggestions to update
    the Google Earth Engine Python code to improve the reliability of the answer
    """
    r = await _assess_factuality_issue(
        question=question,
        python_code=python_code,
        issue_title=issue_title,
        issue_description=issue_description,
        issue_facts=issue_facts,
        issue_question_for_expert=issue_question_for_expert,
    )
    return r
