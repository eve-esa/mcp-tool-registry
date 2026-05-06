"""test using fastmcp client"""

import asyncio

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="http://localhost:8000/mcp",
)

client = Client(transport)


async def main():
    async with client:
        await client.ping()

        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        result = await client.call_tool("geocode_place", {
            "place_name": "Rome",
        })
        print(result)

        result = await client.call_tool("reverse_geocode_place", {
            "place_name": "Rome",
            "latitude": 40.748817,
            "longitude": -73.985428
        })
        print(result)



asyncio.run(main())

