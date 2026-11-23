"""Shared launcher for MCP servers deployed to AgentCore.

Any image can include this package. A server starts it with
``python -m eve_mcp_runtime server.py``. The server module must build
one ``fastmcp.FastMCP`` instance.
"""
