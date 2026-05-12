import asyncio
import os
from contextlib import asynccontextmanager

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport


@asynccontextmanager
async def get_client():
    env = {
        "EVE_EMAIL": os.getenv("EVE_EMAIL", ""),
        "EVE_PASSWORD": os.getenv("EVE_PASSWORD", ""),
    }
    transport = StdioTransport(
        command="poetry",
        args=["run", "python", "-m", "eve_mcp.server"],
        env=env,
    )
    async with Client(transport=transport) as client:
        yield client


async def main():
    async with get_client() as client:
        tools = await client.list_tools()
        print("Available tools:")
        for tool in tools:
            print(f"  - {tool.name}")

        print("\n--- Testing check_eve_health ---")
        result = await client.call_tool("check_eve_health")
        print(result.content[0].text)

        print("\n--- Testing list_eve_collections ---")
        try:
            result = await client.call_tool("list_eve_collections")
            print(result.content[0].text)
        except Exception as e:
            print(f"Error: {e}")

        print("\n--- Testing query_eve (requires auth) ---")
        try:
            result = await client.call_tool(
                "query_eve",
                arguments={"question": "What is Earth observation?"},
            )
            print(result.content[0].text[:500] + "..." if len(result.content[0].text) > 500 else result.content[0].text)
        except Exception as e:
            print(f"Error: {e}")

        print("\n--- Testing extract_factuality_issues ---")
        code = """
import ee
ee.Initialize()

# Load Landsat 8 image
image = ee.Image('LANDSAT/LC08/C01/T1_SR/LC08_123045_20200101_20200101_02_T1')

# Calculate NDVI
ndvi = image.normalizedDifference(['B5', 'B4'])

# Get mean NDVI for the entire image
mean_ndvi = ndvi.reduceRegion(
    reducer=ee.Reducer.mean(),
    geometry=image.geometry(),
    scale=30
)

print(mean_ndvi.getInfo())
"""
        try:
            result = await client.call_tool(
                "extract_factuality_issues",
                arguments={
                    "question": "What is the average NDVI in this Landsat 8 image?",
                    "python_code": code,
                },
            )
            print(result.content[0].text[:1000] if len(result.content[0].text) > 1000 else result.content[0].text)
        except Exception as e:
            print(f"Error: {e}")

        print("\n--- Testing assess_factuality_issue ---")
        try:
            result = await client.call_tool(
                "assess_factuality_issue",
                arguments={
                    "question": "What is the average NDVI in this Landsat 8 image?",
                    "python_code": code,
                    "issue_title": "Incomplete cloud masking",
                    "issue_description": "The code does not apply any cloud masking, which could lead to inaccurate NDVI values for cloudy pixels.",
                    "issue_facts": "Landsat 8 SR products are affected by atmospheric conditions and cloud cover. The code uses raw pixel values without any quality band filtering.",
                    "issue_question_for_expert": "Should a cloud mask be applied to Landsat 8 SR data before calculating NDVI?",
                },
            )
            print(result.content[0].text[:1000] if len(result.content[0].text) > 1000 else result.content[0].text)
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())