"""Tests the functionality in the __main__.py file."""

import importlib
from unittest.mock import patch

from eve_mcp.config import SERVER_MODULE as PKG


class TestMain:
    """Test class for main."""

    @staticmethod
    def test_main_calls_mcp_run_with_stdio():
        """Test that main calls mcp run with stdio."""
        main_mod = importlib.import_module(f"{PKG}.__main__")
        with patch(f"{PKG}.__main__.mcp.run") as mock_run:
            main_mod.main()
        mock_run.assert_called_once_with(transport="stdio")

    @staticmethod
    def test_main_is_callable():
        """Test that main is callable."""
        main_mod = importlib.import_module(f"{PKG}.__main__")
        assert callable(main_mod.main)
