"""Test the EO Dashboard MCP server."""

import asyncio
import os

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

BASE_URL = os.getenv("EODASHBOARD_BASE_URL", "http://localhost:8000/mcp")
TIMEOUT = 60.0


class MCPSession:
    """Bundle MCP HTTP stream contexts into one async context manager."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    async def __aenter__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT))
        self.stream_ctx = streamable_http_client(self.base_url, http_client=self.http)

        self.read, self.write, _ = await self.stream_ctx.__aenter__()

        self.session = ClientSession(self.read, self.write)
        await self.session.__aenter__()
        await self.session.initialize()

        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.__aexit__(exc_type, exc, tb)
        await self.stream_ctx.__aexit__(exc_type, exc, tb)
        await self.http.__aexit__(exc_type, exc, tb)


def _print_block(title: str, content: str, max_chars: int = 1000) -> None:
    print(f"\n=== {title} ===")
    if len(content) <= max_chars:
        print(content)
    else:
        print(content[:max_chars] + "\n... (truncated)")


async def main():
    print(f"Connecting to {BASE_URL}")

    async with MCPSession(BASE_URL) as session:
        tools_result = await session.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        print("Tools:", tool_names)

        expected_tools = {
            "list_narratives",
            "search_narratives",
            "get_narrative_content",
            "list_themes",
            "get_narrative_indicators",
            "search_collections",
            "get_collection_items",
            "find_narratives_for_indicator",
        }
        missing = sorted(expected_tools.difference(tool_names))
        if missing:
            print(f"Missing expected tools: {missing}")
            return

        res_themes = await session.call_tool("list_themes", {})
        _print_block("list_themes", res_themes.content[0].text)

        res_narr = await session.call_tool("search_narratives", {"keyword": "oceans"})
        _print_block("search_narratives(keyword='oceans')", res_narr.content[0].text)

        res_coll = await session.call_tool(
            "search_collections", {"keyword": "co2", "theme": "atmosphere"}
        )
        _print_block(
            "search_collections(keyword='co2', theme='atmosphere')",
            res_coll.content[0].text,
        )

        print("\nEO Dashboard MCP test completed.")


if __name__ == "__main__":
    asyncio.run(main())