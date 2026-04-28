import asyncio
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def create_client(url: str = "http://localhost:8000/mcp") -> Client:
    transport = StreamableHttpTransport(url=url)
    return Client(transport)


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"

    client = create_client(url)

    async with client:
        await client.ping()
        print(f"Connected to {url}")

        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        result = await client.call_tool("extract_data", {
            "description": "Extract person name and email",
            "records": [
                "John Doe can be reached at john@example.com",
                "Contact Jane Smith at jane@company.org"
            ],
            "return_schema": False
        })
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
