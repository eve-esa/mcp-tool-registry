from mcp.server.fastmcp import FastMCP
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

mcp = FastMCP("EOTDL Server", host="0.0.0.0", port=8000, stateless_http=True)

transport = StreamableHttpTransport(
    url="https://mcp.api.eotdl.com/",
)
client = Client(transport)

@mcp.tool()
async def eotdl_search(query: str) -> str:
    async with client:
        result = await client.call_tool("eotdl_search", {
                "query": query,
            })
        if hasattr(result, 'content') and result.content:
            return result.content[0].text if hasattr(result.content[0], 'text') else str(result.content[0])
        return str(result)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EOTDL MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport type (default: streamable-http)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")