"""Tests for the helpers module."""
# pylint: disable=magic-value-comparison,protected-access

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eve_mcp.config import SERVER_MODULE as PKG


class TestExtractXmlTag:
    """Test the extract_xml_tag function."""

    @staticmethod
    def test_extracts_content_between_tags(helpers_mod):
        """Returns the substring between opening and closing tags."""
        text = "noise <foo>payload</foo> trailing"
        assert helpers_mod.extract_xml_tag(text, "foo") == "payload"

    @staticmethod
    def test_raises_when_tag_missing(helpers_mod):
        """Raises NoTagFoundError when the tag is not in the text."""
        with pytest.raises(helpers_mod.NoTagFoundError):
            helpers_mod.extract_xml_tag("nothing to see here", "foo")

    @staticmethod
    def test_raises_when_only_closing_tag_present(helpers_mod):
        """A stray closing tag with no opener is treated as missing."""
        with pytest.raises(helpers_mod.NoTagFoundError):
            helpers_mod.extract_xml_tag("garbage </foo>", "foo")


class TestExtractTag:
    """Test the extract_tag (markdown fence) function."""

    @staticmethod
    def test_extracts_fenced_block(helpers_mod):
        """Returns the content between matching markdown fences."""
        text = 'before ```json {"x": 1} ``` after'
        assert helpers_mod.extract_tag(text, "json").strip() == '{"x": 1}'

    @staticmethod
    def test_extracts_multiline_fenced_block(helpers_mod):
        """DOTALL flag means the inner block can span newlines."""
        text = 'pre\n```json\n{\n  "x": 1\n}\n```\npost'
        out = helpers_mod.extract_tag(text, "json")
        assert '"x": 1' in out

    @staticmethod
    def test_raises_when_no_fence(helpers_mod):
        """Raises NoTagFoundError when the fence is missing."""
        with pytest.raises(helpers_mod.NoTagFoundError):
            helpers_mod.extract_tag("plain text", "json")

    @staticmethod
    def test_does_not_span_multiple_fences(helpers_mod):
        """Non-greedy match stops at the first closing fence."""
        text = (
            'first ```json {"x": 1} ``` middle prose '
            '```json {"y": 2} ``` last'
        )
        captured = helpers_mod.extract_tag(text, "json")
        assert "first" not in captured
        assert '"x": 1' in captured
        assert "middle prose" not in captured
        assert '"y": 2' not in captured


class TestGetEveClient:
    """Test the get_eve_client lazy singleton."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_raises_when_credentials_missing(helpers_mod, monkeypatch):
        """Raises RuntimeError when EVE_EMAIL or EVE_PASSWORD is unset."""
        monkeypatch.delenv("EVE_EMAIL", raising=False)
        monkeypatch.delenv("EVE_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="EVE_EMAIL"):
            await helpers_mod.get_eve_client()

    @pytest.mark.asyncio
    @staticmethod
    async def test_constructs_and_logs_in(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Builds an EVEClient, opens HTTP transport, and logs in."""
        fake_client = MagicMock()
        fake_client.is_authenticated = MagicMock(return_value=True)
        fake_client._ensure_http_client = AsyncMock()
        fake_client.login = AsyncMock()
        with patch(
            f"{PKG}.helpers.EVEClient", return_value=fake_client
        ) as ctor:
            result = await helpers_mod.get_eve_client()
        ctor.assert_called_once_with(base_url="https://eve.example.com")
        fake_client._ensure_http_client.assert_awaited_once()
        fake_client.login.assert_awaited_once_with(
            "test@example.com", "secret"
        )
        assert result is fake_client

    @pytest.mark.asyncio
    @staticmethod
    async def test_returns_cached_authenticated_client(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """A second call returns the cached singleton without re-login."""
        fake_client = MagicMock()
        fake_client.is_authenticated = MagicMock(return_value=True)
        fake_client._ensure_http_client = AsyncMock()
        fake_client.login = AsyncMock()
        with patch(
            f"{PKG}.helpers.EVEClient", return_value=fake_client
        ) as ctor:
            await helpers_mod.get_eve_client()
            await helpers_mod.get_eve_client()
        ctor.assert_called_once()
        fake_client.login.assert_awaited_once()


class TestEnsureLoggedIn:  # pylint: disable=too-few-public-methods
    """Test the ensure_logged_in wrapper."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_calls_ensure_authenticated(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Returns the cached client and refreshes its auth token."""
        fake_client = MagicMock()
        fake_client.is_authenticated = MagicMock(return_value=True)
        fake_client._ensure_http_client = AsyncMock()
        fake_client.login = AsyncMock()
        fake_client.auth = MagicMock()
        fake_client.auth.ensure_authenticated = AsyncMock()
        with patch(f"{PKG}.helpers.EVEClient", return_value=fake_client):
            result = await helpers_mod.ensure_logged_in()
        fake_client.auth.ensure_authenticated.assert_awaited_once()
        assert result is fake_client


def _make_authed_client(stream_events, *, delete_raises=False):
    """Build a fake EVEClient with a stubbed stream + post + delete."""
    client = MagicMock()
    client.is_authenticated = MagicMock(return_value=True)
    client._ensure_http_client = AsyncMock()
    client.login = AsyncMock()
    client.auth = MagicMock()
    client.auth.ensure_authenticated = AsyncMock()
    client.post = AsyncMock(return_value={"id": "conv-123"})
    if delete_raises:
        client.delete = AsyncMock(side_effect=RuntimeError("nope"))
    else:
        client.delete = AsyncMock(return_value=None)

    async def fake_stream(*_args, **_kwargs):
        for event in stream_events:
            yield event

    client.stream = fake_stream
    return client


class TestQueryEve:
    """Test the _query_eve helper."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_assembles_tokens_and_sources(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Streams TOKEN/SOURCE/FINAL events into the result dict."""
        events = [
            {"type": "token", "content": "Hello "},
            {"type": "token", "content": "world"},
            {"type": "source", "data": {"id": "s1"}},
            {"type": "final"},
        ]
        client = _make_authed_client(events)
        with patch(f"{PKG}.helpers.EVEClient", return_value=client):
            raw = await helpers_mod._query_eve(  # pylint: disable=protected-access
                "Q?", collections="c1,c2", k=2
            )
        result = json.loads(raw)
        assert result["answer"] == "Hello world"
        assert result["sources"] == [{"id": "s1"}]
        assert result["conversation_id"] == "conv-123"
        assert "error" not in result
        client.post.assert_awaited_once()
        client.delete.assert_awaited_once_with("/conversations/conv-123")

    @pytest.mark.asyncio
    @staticmethod
    async def test_final_event_content_used_when_no_tokens(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """If only a FINAL event with content arrives, it is the answer."""
        events = [
            {"type": "final", "content": "complete answer"},
        ]
        client = _make_authed_client(events)
        with patch(f"{PKG}.helpers.EVEClient", return_value=client):
            raw = await helpers_mod._query_eve(  # pylint: disable=protected-access
                "Q?"
            )
        assert json.loads(raw)["answer"] == "complete answer"

    @pytest.mark.asyncio
    @staticmethod
    async def test_error_event_populates_error_field(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """An ERROR event short-circuits the stream and is reported."""
        events = [
            {"type": "token", "content": "partial"},
            {"type": "error", "message": "boom"},
            {"type": "token", "content": " ignored"},
        ]
        client = _make_authed_client(events)
        with patch(f"{PKG}.helpers.EVEClient", return_value=client):
            raw = await helpers_mod._query_eve(  # pylint: disable=protected-access
                "Q?"
            )
        result = json.loads(raw)
        assert result["error"] == "boom"
        assert result["answer"] == "partial"

    @pytest.mark.asyncio
    @staticmethod
    async def test_delete_failure_is_swallowed(
        helpers_mod, mock_env, caplog
    ):  # pylint: disable=unused-argument
        """A failure deleting the conversation is logged, not raised."""
        events = [{"type": "final", "content": "ok"}]
        client = _make_authed_client(events, delete_raises=True)
        with patch(f"{PKG}.helpers.EVEClient", return_value=client):
            raw = await helpers_mod._query_eve(  # pylint: disable=protected-access
                "Q?"
            )
        assert json.loads(raw)["answer"] == "ok"
        assert any(
            "Failed to delete conversation" in rec.message
            for rec in caplog.records
        )


class TestExtractFactualityIssues:  # pylint: disable=too-few-public-methods
    """Test the _extract_factuality_issues helper."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_extracts_json_block_from_answer(helpers_mod):
        """Returns a payload with the parsed issues string under 'issues'."""
        inner_json = '[{"title": "x"}]'
        answer = f"intro ```json {inner_json} ``` outro"
        query_payload = json.dumps(
            {"answer": answer, "sources": [], "conversation_id": "c"}
        )
        with patch(
            f"{PKG}.helpers._query_eve",
            new=AsyncMock(return_value=query_payload),
        ):
            raw = await helpers_mod._extract_factuality_issues(  # pylint: disable=protected-access
                "Q?", "code"
            )
        result = json.loads(raw)
        assert inner_json in result["issues"]
        assert result["conversation_id"] == "c"


class TestAssessFactualityIssue:
    """Test the _assess_factuality_issue helper."""

    @staticmethod
    async def _run_assess(helpers_mod, answer):
        """Drive _assess_factuality_issue with a stubbed _query_eve."""
        query_payload = json.dumps(
            {"answer": answer, "sources": [], "conversation_id": "c"}
        )
        with patch(
            f"{PKG}.helpers._query_eve",
            new=AsyncMock(return_value=query_payload),
        ):
            raw = await helpers_mod._assess_factuality_issue(
                "Q?", "code", "t", "d", "f", "eq"
            )
        return json.loads(raw)

    @pytest.mark.asyncio
    @staticmethod
    async def test_strips_full_recommendations_block_from_answer(
        helpers_mod,
    ):
        """Removes the entire <CODE_RECOMMENDATIONS>...</...> block,
        tags included, from the answer body."""
        recs = '{"r1": {"explanation": "e", "code_snippet": "x = 1"}}'
        answer = (
            "Markdown body. "
            f"<CODE_RECOMMENDATIONS>{recs}</CODE_RECOMMENDATIONS>"
            " trailer"
        )
        result = await TestAssessFactualityIssue._run_assess(
            helpers_mod, answer
        )
        assert "<CODE_RECOMMENDATIONS>" not in result["answer"]
        assert "</CODE_RECOMMENDATIONS>" not in result["answer"]
        assert recs not in result["answer"]
        assert "Markdown body." in result["answer"]
        assert "trailer" in result["answer"]
        assert result["code_recommendations"] == recs

    @pytest.mark.asyncio
    @staticmethod
    async def test_unwraps_inner_json_fence_and_cleans_answer(
        helpers_mod,
    ):
        """If <CODE_RECOMMENDATIONS> wraps a ```json fence, the inner
        JSON is unwrapped AND the wrapping tags + fence are removed
        from the answer."""
        inner = '{"r1": {"explanation": "e", "code_snippet": "x = 1"}}'
        recs_with_fence = f"```json {inner} ```"
        answer = (
            "Body. "
            f"<CODE_RECOMMENDATIONS>{recs_with_fence}</CODE_RECOMMENDATIONS>"
        )
        result = await TestAssessFactualityIssue._run_assess(
            helpers_mod, answer
        )
        assert inner in result["code_recommendations"]
        assert "<CODE_RECOMMENDATIONS>" not in result["answer"]
        assert "```json" not in result["answer"]
        assert "Body." in result["answer"]

    @pytest.mark.asyncio
    @staticmethod
    async def test_returns_empty_recommendations_when_tag_absent(
        helpers_mod,
    ):
        """If the LLM omits <CODE_RECOMMENDATIONS>, the tool returns
        an empty recommendations field rather than raising."""
        answer = "Body of the assessment with no recommendations section."
        result = await TestAssessFactualityIssue._run_assess(
            helpers_mod, answer
        )
        assert result["code_recommendations"] == ""
        assert result["answer"] == answer

    @pytest.mark.asyncio
    @staticmethod
    async def test_keeps_malformed_inner_fence_as_is(
        helpers_mod,
    ):
        """If the inner fence claims to be json but doesn't close,
        leave the recommendations as the raw block content."""
        broken = '```json {"r1": 1}'  # missing closing fence
        answer = (
            "Body. " f"<CODE_RECOMMENDATIONS>{broken}</CODE_RECOMMENDATIONS>"
        )
        result = await TestAssessFactualityIssue._run_assess(
            helpers_mod, answer
        )
        assert "```json" in result["code_recommendations"]
        assert "<CODE_RECOMMENDATIONS>" not in result["answer"]


def test_helpers_module_importable():
    """Sanity: the helpers module imports cleanly under PKG."""
    helpers = importlib.import_module(f"{PKG}.helpers")
    assert hasattr(helpers, "_query_eve")
