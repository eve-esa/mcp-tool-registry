"""This file contains fixtures for testing the server."""

import importlib
import sys

import pytest
from fastmcp.client import Client

from eve_mcp.config import SERVER_MODULE as PKG
from eve_mcp.server import mcp


@pytest.fixture
async def main_mcp_client():
    """A FastMCP Client context manager to use for testing the server."""
    async with Client(transport=mcp) as mcp_client:
        yield mcp_client


@pytest.fixture(autouse=True)
def reset_eve_client_singleton():
    """Reset the module-level _eve_client singleton before every test."""
    if (helpers := sys.modules.get(f"{PKG}.helpers")) is not None:
        helpers._eve_client = None  # pylint: disable=protected-access
    yield
    if helpers is not None:
        helpers._eve_client = None  # pylint: disable=protected-access


@pytest.fixture()
def helpers_mod():
    """Import helpers module."""
    return importlib.import_module(f"{PKG}.helpers")


@pytest.fixture()
def mock_env(monkeypatch):
    """Monkeypatch env vars."""
    monkeypatch.setenv("EVE_EMAIL", "test@example.com")
    monkeypatch.setenv("EVE_PASSWORD", "secret")
    monkeypatch.setenv("EVE_BASE_URL", "https://eve.example.com")
