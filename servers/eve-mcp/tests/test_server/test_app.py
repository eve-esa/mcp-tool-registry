"""Tests the functionality in the app.py file."""

import importlib

from fastmcp import FastMCP

from eve_mcp.config import SERVER_MODULE as PKG


class TestApp:
    """Test the app."""

    @staticmethod
    def test_mcp_instance_name():
        """Test that the mcp instance has th correct name."""
        expected_name = "eve-mcp"
        app = importlib.import_module(f"{PKG}.app")
        assert app.mcp.name == expected_name

    @staticmethod
    def test_mcp_is_fastmcp_instance():
        """Test that the mcp object is a FastMCP instance."""
        app = importlib.import_module(f"{PKG}.app")
        assert isinstance(app.mcp, FastMCP)
