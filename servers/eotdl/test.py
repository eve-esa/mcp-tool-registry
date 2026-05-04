import asyncio

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main():
    transport = StreamableHttpTransport(
        url="http://localhost:8000/mcp",
    )
    client = Client(transport)

    async with client:
        result = await client.call_tool("eotdl_search", {
            "query": "test",
        })
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
