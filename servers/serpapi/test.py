"""Test the SerpAPI MCP server."""

import asyncio
import json
import os

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

BASE_URL = "http://localhost:8000/mcp"
TIMEOUT = 30.0
API_KEY = os.getenv("SERPAPI_API_KEY", "")


class MCPSession:
    """bundle all the 3 context managers into a single context manager"""
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    async def __aenter__(self):
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT),
            headers={"X-API-Key": self.api_key}
        )
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


async def main():
    if not API_KEY:
        print("Error: SERPAPI_API_KEY not set in .env")
        return

    async with MCPSession(BASE_URL, API_KEY) as session:
        tools_result = await session.list_tools()
        print("Tools:", [t.name for t in tools_result.tools])

        result = await session.call_tool("search_google", {
            "query": "Python programming language",
            "num": 5
        })

        data = json.loads(result.content[0].text)

        if "error" in data:
            print(f"Error: {data['error']}")
            return

        print(f"Got {len(data['results'])} results:")
        for i, r in enumerate(data["results"], 1):
            print(f"{i}. {r['title']}")
            print(f"   {r['url']}")
            if r["snippet"]:
                print(f"   {r['snippet'][:100]}...")
            print()


if __name__ == "__main__":
    asyncio.run(main())
