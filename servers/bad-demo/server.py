"""Bad demo server — intentionally breaks PR rules."""

from mcp.server.fastmcp import FastMCP

# RULE VIOLATION 1: wrong host (should be 0.0.0.0)
# RULE VIOLATION 2: wrong port (should be 8000)
# RULE VIOLATION 3: missing stateless_http=True
mcp = FastMCP("Bad Demo", host="127.0.0.1", port=3000)

# RULE VIOLATION 4: hardcoded password
API_PASSWORD = "SuperSecretPassword123"

# RULE VIOLATION 5: hardcoded API key
MY_API_KEY = "sk-proj-abcdef1234567890ghijklmnop"


@mcp.tool()
async def ping() -> str:
    return "pong"


# RULE VIOLATION 6: missing streamable-http transport
if __name__ == "__main__":
    mcp.run(transport="sse")
