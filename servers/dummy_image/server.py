from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
import base64
from io import BytesIO
from PIL import Image

mcp = FastMCP("dummy image")

@mcp.tool()
def get_sample_image(color: str = "blue") -> ImageContent:
    img = Image.new("RGB", (10000, 10000), color=color)
    
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    
    img_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    return ImageContent(
        type="image",
        data=img_base64,
        mimeType="image/png"
    )

if __name__ == "__main__":
    mcp.run(transport="streamable-http")