"""Entry point for ``python -m eve_mcp.server``."""

from . import mcp


def main():
    """Run the mcp server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
