"""EVE collection listing and health-check tools."""

import json
import logging

from .app import mcp
from .constants import DEFAULT_BASE_URL
from .helpers import ensure_logged_in

logger = logging.getLogger(__name__)


@mcp.tool()
async def list_eve_collections() -> str:
    """List publicly available EVE document collections.

    Returns:
        JSON string containing the list of public collections.
    """
    client = await ensure_logged_in()
    logger.info("Listing public EVE collections")
    result = await client.get("/collections/public")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def check_eve_health() -> str:
    """Check the EVE API health status (unauthenticated).

    Returns:
        JSON string with the health-check response.
    """
    import os  # pylint: disable=import-outside-toplevel

    import httpx  # pylint: disable=import-outside-toplevel

    base_url = os.getenv("EVE_BASE_URL", DEFAULT_BASE_URL)
    logger.info("Checking EVE health at %s", base_url)

    async with httpx.AsyncClient(timeout=10.0) as http:
        response = await http.get(f"{base_url}/health")
        response.raise_for_status()
        return json.dumps(response.json(), ensure_ascii=False)
