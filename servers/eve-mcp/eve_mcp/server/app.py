"""FastMCP application instance and logging configuration."""

# import logging
# import os

from fastmcp import FastMCP

mcp = FastMCP("eve-mcp", host="0.0.0.0", port=8000, stateless_http=True)
