"""Dummy image MCP server: returns a solid-color PNG as inline ImageContent.

A stand-in for real image-producing tools, used to exercise the EVE artifact
capture pipeline end to end (base64 ImageContent -> S3 artifact -> inline chat
render) without any external dependency.
"""

import base64
from io import BytesIO

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
from PIL import Image

# stateless_http is required on AgentCore Runtime: each invocation may be routed
# to a different microVM, so the server cannot depend on the stateful
# streamable-http session handshake (a stateful server answers every external
# request with 421 Misdirected Request through the AgentCore adapter).
mcp = FastMCP("dummy image", host="0.0.0.0", port=8000, stateless_http=True)


@mcp.tool()
def get_sample_image(color: str = "blue", size: int = 512) -> ImageContent:
    """Generate a solid-color square PNG and return it inline as base64.

    Args:
        color: Any color name or hex string Pillow understands.
        size: Side of the square in pixels (capped at 2048 to keep the
            payload and the microVM memory footprint sane).
    """
    side = max(1, min(int(size), 2048))
    img = Image.new("RGB", (side, side), color=color)

    buffered = BytesIO()
    img.save(buffered, format="PNG")

    img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return ImageContent(
        type="image",
        data=img_base64,
        mimeType="image/png",
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
