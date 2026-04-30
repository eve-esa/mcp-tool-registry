"""
EO Dashboard Server
======================
Searches narratives, themes and indicators from the joint ESA / NASA / JAXA EO Dashboard and resolves each result to its underlying STAC catalogue items for direct data access.

Tools:
    list_narratives: Get a list of all available EODashboard narratives
    search_narratives: Search narratives by keyword in title or theme
    get_narrative_content: Get full markdown content of a narrative by URL
    list_themes: Get all available themes from narratives
    get_narrative_indicators: Get collection indicator codes referenced in a narrative
    search_collections: Search STAC collections by code, theme, or keyword
    get_collection_items: Get sample data items from a STAC collection via GeoParquet
    find_narratives_for_indicator: Reverse lookup narratives by collection code

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]>=1.2.0" requests pystac pyarrow python-dotenv
"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urljoin

import pyarrow.parquet as pq
import pystac
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP("EO Dashboard Server", host="0.0.0.0", port=8000, stateless_http=True)

NARRATIVES_URL = (
    "https://esa-eodashboards.github.io/eodashboard-narratives/narratives.json"
)
STAC_CATALOG_URL = (
    "https://esa-eodashboards.github.io/eodashboard-catalog/trilateral/catalog.json"
)


class EODashboardCache:
    """Cache for narratives and STAC catalog data."""

    def __init__(self):
        self.narratives_index = None
        self.narrative_to_indicators = None
        self.code_to_collection = None
        self.root_catalog = None

    def load_narratives(self):
        if self.narratives_index is not None:
            return

        response = requests.get(NARRATIVES_URL, timeout=30)
        response.raise_for_status()
        self.narratives_index = response.json()

        self.narrative_to_indicators = {}
        for narrative in self.narratives_index:
            try:
                self._fetch_narrative(narrative["file"])
                key = narrative.get("title", "Untitled").lower().strip()
                self.narrative_to_indicators[key] = {
                    "theme": narrative.get("theme", "unknown"),
                    "tags": narrative.get("tags", ""),
                    "url": narrative["file"],
                    "date": narrative.get("date", ""),
                    "official": narrative.get("official", False),
                    "indicators": narrative.get("collections", []),
                }
            except Exception:
                continue

    def load_catalog(self):
        if self.root_catalog is not None:
            return

        self.root_catalog = pystac.Catalog.from_file(STAC_CATALOG_URL)
        self.code_to_collection = {}

        for link in self.root_catalog.links:
            if link.rel != "child":
                continue

            code = link.extra_fields.get("code")
            if not code:
                continue

            full_url = requests.compat.urljoin(STAC_CATALOG_URL, link.href)
            self.code_to_collection[code] = {
                "code": code,
                "title": link.extra_fields.get("title", code),
                "themes": link.extra_fields.get("themes", []),
                "tags": link.extra_fields.get("tags", []),
                "href": link.href,
                "full_url": full_url,
                "self_href": full_url,
                "satellite": link.extra_fields.get("satellite"),
                "sensor": link.extra_fields.get("sensor"),
                "description": link.extra_fields.get("description", ""),
            }

    @staticmethod
    def _fetch_narrative(url: str) -> str:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text


cache = EODashboardCache()


def get_theme(theme_obj: str | list) -> str:
    if isinstance(theme_obj, str):
        return theme_obj.lower()
    if isinstance(theme_obj, list) and theme_obj:
        return str(theme_obj[0]).lower()
    return "unknown"


@mcp.tool()
def list_narratives() -> str:
    """Get a list of all available EODashboard narratives."""
    cache.load_narratives()
    narratives = cache.narratives_index

    result = f"Found {len(narratives)} narratives:\n\n"
    for narr in narratives[:10]:
        result += f"- {narr.get('title', 'Untitled')}\n"
        result += f"  Theme: {get_theme(narr.get('theme', 'unknown'))}\n"
        result += f"  URL: {narr.get('file', '')}\n\n"

    if len(narratives) > 10:
        result += f"... and {len(narratives) - 10} more"

    return result


@mcp.tool()
def search_narratives(keyword: str) -> str:
    """Search narratives by keyword in title or theme."""
    if not keyword:
        return "You must provide a keyword to search."

    cache.load_narratives()
    narratives = cache.narratives_index
    keyword_lower = keyword.lower()

    filtered = []
    for narr in narratives:
        title = narr.get("title", "").lower()
        narr_theme = get_theme(narr.get("theme", ""))
        if keyword_lower in title or keyword_lower in narr_theme:
            filtered.append(narr)

    if not filtered:
        return f"No narratives found matching keyword='{keyword}'"

    result = f"Found {len(filtered)} matching narrative(s):\n\n"
    for narr in filtered[:15]:
        title = narr.get("title", "Untitled")
        cache_key = title.lower().strip()
        indicators = cache.narrative_to_indicators.get(cache_key, {}).get(
            "indicators", []
        )

        result += f"- {title}\n"
        result += f"  Theme: {get_theme(narr.get('theme', 'unknown'))}\n"
        result += f"  Tags: {narr.get('tags', 'none')}\n"
        result += f"  Date: {narr.get('date', 'unknown')}\n"
        result += f"  Indicators: {', '.join(indicators) if indicators else 'None'}\n"
        result += f"  URL: {narr.get('file', '')}\n"
        result += f"  STAC Collection codes: [{', '.join(narr.get('collections', [])) if narr.get('collections') else 'None'}]\n\n"

    if len(filtered) > 15:
        result += f"... and {len(filtered) - 15} more"

    return result


@mcp.tool()
def get_narrative_content(url: str) -> str:
    """Get the full content of a specific narrative by its URL."""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.text

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                body = parts[2].strip()
                if len(body) > 4000:
                    body = (
                        body[:4000]
                        + "\n\n... (content truncated, showing first 4000 chars)"
                    )
                return f"FRONTMATTER:\n{frontmatter}\n\nCONTENT:\n{body}"

        if len(content) > 5000:
            content = (
                content[:5000] + "\n\n... (content truncated, showing first 5000 chars)"
            )

        return content
    except Exception as e:
        return f"Error fetching narrative: {str(e)}"


@mcp.tool()
def list_themes() -> str:
    """Get all available themes from narratives."""
    cache.load_narratives()
    narratives = cache.narratives_index

    themes = set()
    for narr in narratives:
        theme = narr.get("theme", "")
        if theme:
            themes.add(get_theme(theme))

    result = f"Available themes ({len(themes)}):\n\n"
    for theme in sorted(themes):
        count = sum(1 for n in narratives if get_theme(n.get("theme", "")) == theme)
        result += f"- {theme} ({count} narratives)\n"

    return result


@mcp.tool()
def get_narrative_indicators(narrative_title: str) -> str:
    """Get collection codes referenced in a narrative."""
    cache.load_narratives()
    cache.load_catalog()

    narrative_info = cache.narrative_to_indicators.get(narrative_title.lower().strip())
    if not narrative_info:
        for nar_title in cache.narrative_to_indicators:
            if narrative_title.lower().strip() in nar_title.lower().strip():
                narrative_info = cache.narrative_to_indicators[nar_title]
                narrative_title = nar_title
                break

    if not narrative_info:
        return f"Narrative '{narrative_title}' not found. Use search_narratives to find exact titles."

    indicators = narrative_info["indicators"]
    if not indicators:
        return f"Narrative '{narrative_title}' does not reference any collection indicators."

    response = (
        f"Narrative '{narrative_title}' references {len(indicators)} collection(s):\n\n"
    )
    for code in indicators:
        collection = cache.code_to_collection.get(code)
        if collection:
            response += f"   {code}\n"
            response += f"   Title: {collection['title']}\n"
            response += f"   Themes: {', '.join(collection['themes'])}\n"
            if collection.get("satellite"):
                response += f"   Satellite: {collection['satellite']}\n"
            response += f"   URL: {collection['full_url']}\n\n"
        else:
            response += f"    {code} - Not found in catalog\n\n"

    return response


@mcp.tool()
def search_collections(code: str = "", theme: str = "", keyword: str = "") -> str:
    """Search STAC collections by code, theme, or keyword."""
    cache.load_catalog()

    code = code.upper()
    theme = theme.lower()
    keyword = keyword.lower()

    results = []
    for coll_code, collection in cache.code_to_collection.items():
        if code and code != coll_code:
            continue

        if theme:
            coll_themes = [t.lower() for t in collection.get("themes", [])]
            if theme not in coll_themes:
                continue

        if keyword:
            searchable = (
                f"{collection['title']} {collection.get('description', '')}".lower()
            )
            if keyword not in searchable:
                continue

        results.append(collection)

    if not results:
        return "No collections found matching the criteria."

    response = f"Found {len(results)} collection(s):\n\n"
    for coll in results[:15]:
        response += f"   {coll['code']}\n"
        response += f"   Title: {coll['title']}\n"
        response += f"   Themes: {', '.join(coll['themes'])}\n"
        if coll.get("satellite"):
            response += f"   Satellite: {coll['satellite']}\n"
        if coll.get("sensor"):
            response += f"   Sensor: {coll['sensor']}\n"
        response += f"   URL: {coll['self_href']}\n\n"

    if len(results) > 15:
        response += f"... and {len(results) - 15} more results (showing first 15)"

    return response


@mcp.tool()
def get_collection_items(collection_code: str, limit: int = 10) -> str:
    """Get data items from a specific STAC collection."""
    cache.load_catalog()

    collection = cache.code_to_collection.get(collection_code)
    if not collection:
        collection = cache.code_to_collection.get(collection_code.upper())

    if not collection:
        return f"Collection '{collection_code}' not found. Use search_collections to find valid codes."

    try:
        pystac_collection = pystac.Collection.from_file(collection["self_href"])
        child_link = [link for link in pystac_collection.links if link.rel == "child"]
        child_href = collection["self_href"]

        while child_link:
            child_href = urljoin(collection["self_href"], str(child_link[0].target))
            pystac_collection = pystac.Collection.from_file(child_href)
            child_link = [
                link for link in pystac_collection.links if link.rel == "child"
            ]

        geoparquet_asset = (
            pystac_collection.assets.get("geoparquet") if pystac_collection else None
        )
        geoparquet_link = geoparquet_asset.href if geoparquet_asset else None

        if not geoparquet_link:
            return (
                f"Collection '{collection['code']}' does not have a geoparquet asset. "
                "Items may be in a different format."
            )

        geoparquet_link = urljoin(child_href, geoparquet_link)
        response = requests.get(geoparquet_link, timeout=60)
        response.raise_for_status()

        table = pq.read_table(io.BytesIO(response.content))
        df = table.to_pandas()

        response_text = f"Collection '{collection['code']}' - {collection['title']}\n"
        response_text += (
            f"Found {len(df)} item(s) (showing first {min(limit, len(df))}):\n\n"
        )

        for idx, row in df.head(limit).iterrows():
            response_text += f"Item {idx + 1}:\n"
            response_text += f"  Properties: {dict(row)}\n\n"

        return response_text
    except Exception as e:
        return f"Error retrieving items for collection '{collection['code']}': {str(e)}"


@mcp.tool()
def find_narratives_for_indicator(indicator_code: str) -> str:
    """Reverse lookup narratives that reference a collection code."""
    cache.load_narratives()
    code = indicator_code.upper()

    matching = []
    for title, info in cache.narrative_to_indicators.items():
        if code in [ind.upper() for ind in info["indicators"]]:
            matching.append(
                {
                    "title": title,
                    "theme": info["theme"],
                    "tags": info["tags"],
                    "url": info["url"],
                }
            )

    if not matching:
        return f"No narratives found referencing indicator '{code}'."

    response = f"Found {len(matching)} narrative(s) referencing '{code}':\n\n"
    for narr in matching:
        response += f"   {narr['title']}\n"
        response += f"   Theme: {narr['theme']} | Tags: {narr['tags']}\n"
        response += f"   URL: {narr['url']}\n\n"

    return response


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EO Dashboard Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
