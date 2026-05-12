"""Tests the functionality in the tools_query.py file."""

import importlib
import json
from unittest.mock import AsyncMock, patch

import pytest

from eve_mcp.config import SERVER_MODULE as PKG


class TestQueryEve:
    """Test the query_eve function."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_query_eve_delegates_to_helper(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Test the query_eve function delegates to a helper."""
        tools_q = importlib.import_module(f"{PKG}.tools_query")
        expected = json.dumps(
            {"answer": "42", "sources": [], "conversation_id": "x"}
        )
        with patch(
            f"{PKG}.tools_query._query_eve",
            new=AsyncMock(return_value=expected),
        ) as m:
            result = await tools_q.query_eve("What is life?", "col-a", 3)
        m.assert_awaited_once_with(
            question="What is life?", collections="col-a", k=3
        )
        assert result == expected

    @pytest.mark.asyncio
    @staticmethod
    async def test_query_eve_uses_defaults(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Test that query_eve uses defaults."""
        tools_q = importlib.import_module(f"{PKG}.tools_query")
        constants = importlib.import_module(f"{PKG}.constants")
        expected = json.dumps(
            {"answer": "", "sources": [], "conversation_id": "y"}
        )
        with patch(
            f"{PKG}.tools_query._query_eve",
            new=AsyncMock(return_value=expected),
        ) as m:
            await tools_q.query_eve("q")
        _, kwargs = m.call_args
        assert kwargs["collections"] == constants.DEFAULT_COLLECTION
        assert kwargs["k"] == constants.DEFAULT_K


class TestExtractFactualityIssues:  # pylint: disable=too-few-public-methods
    """Test the _extract_factuality_issues function."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_extract_factuality_issues_delegates(
        mock_env,
    ):  # pylint: disable=unused-argument
        """Test that _extract_factuality_issues delegates to a helper."""
        tools_q = importlib.import_module(f"{PKG}.tools_query")
        expected = json.dumps({"issues": "[]"})
        with patch(
            f"{PKG}.tools_query._extract_factuality_issues",
            new=AsyncMock(return_value=expected),
        ) as m:
            result = await tools_q.extract_factuality_issues("q", "code")
        m.assert_awaited_once_with(question="q", python_code="code")
        assert result == expected


class TestAssessFactualityIssue:  # pylint: disable=too-few-public-methods
    """Test the _assess_factuality_issue function."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_assess_factuality_issue_delegates(
        mock_env,
    ):  # pylint: disable=unused-argument
        """Test that _assess_factuality_issue delegates to a helper."""
        tools_q = importlib.import_module(f"{PKG}.tools_query")
        expected = json.dumps({"code_recommendations": "{}"})
        with patch(
            f"{PKG}.tools_query._assess_factuality_issue",
            new=AsyncMock(return_value=expected),
        ) as m:
            result = await tools_q.assess_factuality_issue(
                "q", "code", "t", "d", "f", "eq"
            )
        m.assert_awaited_once_with(
            question="q",
            python_code="code",
            issue_title="t",
            issue_description="d",
            issue_facts="f",
            issue_question_for_expert="eq",
        )
        assert result == expected
