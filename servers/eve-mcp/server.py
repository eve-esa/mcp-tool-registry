"""Entry point for the EVE MCP server."""

from eve_mcp.server import mcp

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