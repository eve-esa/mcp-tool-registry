"""Tests the functionality in the __init__.py file."""

import importlib

import pytest
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from eve_mcp.config import SERVER_MODULE as PKG

EXPECTED_TOOLS = {
    "check_eve_health",
    "list_eve_collections",
    "query_eve",
    "extract_factuality_issues",
    "assess_factuality_issue",
}


class TestInit:
    """Test the init."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_all_tools_registered(
        main_mcp_client: Client[FastMCPTransport],
    ):
        """Test that all tools are imported."""
        importlib.import_module(PKG)
        tools = await main_mcp_client.list_tools()
        registered = set(tool.name for tool in tools)
        assert registered == EXPECTED_TOOLS

    @staticmethod
    def test_mcp_exported():
        """Test that mcp instance is imported."""
        pkg = importlib.import_module(PKG)
        assert isinstance(pkg.mcp, FastMCP)

    @staticmethod
    def test_tool_functions_importable():
        """Test that tool functions are importable."""
        pkg = importlib.import_module(PKG)
        for name in EXPECTED_TOOLS:
            assert hasattr(pkg, name), f"Missing export: {name}"
