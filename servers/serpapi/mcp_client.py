"""test using fastmcp client"""

import asyncio
import os

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

load_dotenv()

API_KEY = os.getenv("SERPAPI_API_KEY", "")

transport = StreamableHttpTransport(
    url="http://localhost:8000/mcp",
    headers={"X-API-Key": API_KEY}
)
client = Client(transport)


async def main():
    async with client:
        await client.ping()

        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        result = await client.call_tool("search_google", {
            "query": "Python programming language",
            "num": 5
        })
        print(result)


asyncio.run(main())
