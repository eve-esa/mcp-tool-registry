"""test using fastmcp client"""
import asyncio
import base64

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
        result = await client.call_tool("get_sample_image", {
            "color": "red",
            "size": 256,
        })

        for item in result.content:
            if item.type == "image":
                print(f"Success! Received {item.mimeType}")
                with open("output.png", "wb") as f:
                    f.write(base64.b64decode(item.data))
                print("Image saved to 'output.png'")
            else:
                print("Unexpected content:", item)


if __name__ == "__main__":
    asyncio.run(main())
