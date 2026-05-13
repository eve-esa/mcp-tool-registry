import asyncio
import os

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

load_dotenv()

API_KEY = os.getenv("GROQ_API_KEY", "")

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
