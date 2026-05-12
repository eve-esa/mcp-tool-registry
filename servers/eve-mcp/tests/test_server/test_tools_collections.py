"""Tests the functionality in the tools_collections.py file."""

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from eve_mcp.config import SERVER_MODULE as PKG


class TestListEveCollections:
    """Test the list_eve_collections function."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_list_eve_collections_returns_json(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Test that list_eve_collections returns a json."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value={"collections": ["c1", "c2"]})
        helpers_mod.ensure_logged_in = AsyncMock(return_value=mock_client)
        # Patch ensure_logged_in inside tools_collections' namespace
        with patch(
            f"{PKG}.tools_collections.ensure_logged_in",
            new=helpers_mod.ensure_logged_in,
        ):
            result = await tools_col.list_eve_collections()
        data = json.loads(result)
        assert data == {"collections": ["c1", "c2"]}

    @pytest.mark.asyncio
    @staticmethod
    async def test_list_eve_collections_calls_correct_endpoint(
        helpers_mod, mock_env
    ):  # pylint: disable=unused-argument
        """Test that list_eve_collections calls the correct endpoint."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=[])
        helpers_mod.ensure_logged_in = AsyncMock(return_value=mock_client)
        with patch(
            f"{PKG}.tools_collections.ensure_logged_in",
            new=helpers_mod.ensure_logged_in,
        ):
            await tools_col.list_eve_collections()
        mock_client.get.assert_awaited_once_with("/collections/public")


class TestCheckEveHealth:
    """Test the check_eve_health function."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_check_eve_health_returns_json(monkeypatch):
        """Test the check_eve_health returns json."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        monkeypatch.setenv("EVE_BASE_URL", "https://eve.example.com")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"status": "ok"})

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_http):
            result = await tools_col.check_eve_health()

        assert json.loads(result) == {"status": "ok"}

    @pytest.mark.asyncio
    @staticmethod
    async def test_check_eve_health_uses_base_url_env(monkeypatch):
        """Test the check_eve_health uses base url env var."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        monkeypatch.setenv("EVE_BASE_URL", "https://custom.eve.io")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_http):
            await tools_col.check_eve_health()

        called_url = mock_http.get.call_args[0][0]
        assert called_url.startswith("https://custom.eve.io")

    @pytest.mark.asyncio
    @staticmethod
    async def test_check_eve_health_raises_on_http_error(monkeypatch):
        """Test the check_eve_health raises on http error."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        monkeypatch.setenv("EVE_BASE_URL", "https://eve.example.com")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
        )

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_http):
            with pytest.raises(httpx.HTTPStatusError):
                await tools_col.check_eve_health()

    @pytest.mark.asyncio
    @staticmethod
    async def test_check_eve_health_default_base_url(monkeypatch):
        """Test the check_eve_health defaults to base url."""
        tools_col = importlib.import_module(f"{PKG}.tools_collections")
        monkeypatch.delenv("EVE_BASE_URL", raising=False)
        constants = importlib.import_module(f"{PKG}.constants")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={})

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_http):
            await tools_col.check_eve_health()

        called_url = mock_http.get.call_args[0][0]
        assert called_url.startswith(constants.DEFAULT_BASE_URL)
