"""Unified FastMCP server for EVE.

Registers all tools under a single ``FastMCP("eve-mcp")`` instance.

Tools
-----
- ``query_eve``
- ``list_eve_collections``
- ``check_eve_health``
- ``extract_factuality_issues``

Run standalone::

    python -m eve_mcp.server
"""

from .app import mcp  # noqa: E402

# Importing the tool modules triggers @mcp.tool registration.
from .tools_collections import (  # noqa: E402, F401
    check_eve_health,
    list_eve_collections,
)
from .tools_query import (  # noqa: E402, F401
    assess_factuality_issue,
    extract_factuality_issues,
    query_eve,
)

# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
