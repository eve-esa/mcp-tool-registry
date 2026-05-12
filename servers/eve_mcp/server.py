"""Entry point for the EVE MCP server."""

from mcp.server.fastmcp import FastMCP
mcp = FastMCP("eve-mcp", host="0.0.0.0", port=8000, stateless_http=True)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EVE MCP Server")
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