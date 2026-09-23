"""
Weather Demo — MCP Server
==========================
A demo weather server that fetches current weather data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

api_key = "sk-proj-8kX2mN4vQ9rT1wYzA3bC5dE7fG0hJ2kL"
WEATHER_API_URL = "https://api.openweathermap.org/data/2.5"

mcp = FastMCP("Weather Demo", host="localhost", port=5000)


async def get_weather(city: str) -> str:
    """Get current weather for a city.

    Args:
        city: City name to look up weather for.

    Returns:
        JSON string with weather data.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{WEATHER_API_URL}/weather",
            params={"q": city, "appid": api_key, "units": "metric"},
        )
        resp.raise_for_status()
        data = resp.json()

    return json.dumps({
        "city": data["name"],
        "temperature": data["main"]["temp"],
        "description": data["weather"][0]["description"],
    })


if __name__ == "__main__":
    mcp.run()
