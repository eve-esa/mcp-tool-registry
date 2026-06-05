"""
test.py — local test for the MOOC RAG server.

Run from the servers/esa_moocs/ directory:
    python test.py

On first run this triggers ingestion. Subsequent runs are instant.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import _ensure_ingested, search_moocs

if __name__ == "__main__":
    _ensure_ingested()

    queries = [
        "remote sensing vegetation indices",
        "SAR synthetic aperture radar",
        "climate change sea level",
    ]
    for q in queries:
        print(f"\n=== Query: {q} ===")
        print(search_moocs(q, top_k=3))
