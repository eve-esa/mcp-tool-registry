import asyncio

from fastmcp import Client

client = Client("http://localhost:8000/mcp")

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
