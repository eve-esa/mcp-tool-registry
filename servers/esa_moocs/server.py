"""
ESA MOOCs RAG MCP Server
========================
Semantic search over Earth Observation MOOC transcripts using a local
Qdrant vector store.

On first startup the server embeds and indexes all transcript chunks from
the bundled data/MOOC.jsonl. Subsequent starts are instant.

Tools:
    search_moocs — retrieve the most relevant MOOC transcript chunks

Usage:
    python server.py                               # HTTP transport (default)
    python server.py --transport stdio             # stdio for MCP clients
    python server.py --transport http --port 8000  # explicit HTTP

Requirements:
    pip install -r requirements.txt
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_SERVER_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

COLLECTION = "esa_moocs"
EMBED_MODEL = "nasa-impact/nasa-smd-ibm-st-v2"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

mcp = FastMCP("ESA MOOCs RAG", host="0.0.0.0", port=8000, stateless_http=True)

_embedder = None
_reranker = None
_qdrant = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model %s", EMBED_MODEL)
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        logger.info("Loading reranker %s", RERANK_MODEL)
        _reranker = CrossEncoder(RERANK_MODEL)
    return _reranker


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(path=str(_SERVER_DIR / "qdrant_storage"))
    return _qdrant


def _ensure_ingested() -> None:
    """Run ingestion if the collection is missing or empty."""
    from qdrant_client.http.exceptions import UnexpectedResponse
    client = _get_qdrant()
    try:
        info = client.get_collection(COLLECTION)
        if info.points_count and info.points_count > 0:
            logger.info("Collection '%s' has %d points — skipping ingest", COLLECTION, info.points_count)
            return
    except (UnexpectedResponse, Exception):
        pass

    logger.info("Collection '%s' not found or empty — running ingestion", COLLECTION)
    import ingest
    ingest.run(_SERVER_DIR)


def _retrieve(query: str, top_k: int = 20) -> list[tuple[float, dict]]:
    vector = _get_embedder().encode(query).tolist()
    results = _get_qdrant().query_points(
        collection_name=COLLECTION, query=vector, limit=top_k
    )
    return [(r.score, r.payload) for r in results.points]


def _rerank(query: str, hits: list[tuple[float, dict]], top_k: int = 5) -> list[tuple[float, dict]]:
    pairs = [(query, h[1].get("content", "")) for h in hits]
    scores = _get_reranker().predict(pairs)
    ranked = sorted(
        zip(scores.tolist(), [h[1] for h in hits], strict=False),
        reverse=True,
    )
    return ranked[:top_k]


def _format(hits: list[tuple[float, dict]]) -> str:
    if not hits:
        return "No relevant MOOC content found."
    lines = []
    for i, (score, payload) in enumerate(hits, start=1):
        title = payload.get("title", "unknown")
        lines.append(f"[{i}] {title} — rerank score: {score:.4f}")
        lines.append(payload.get("content", ""))
        lines.append("---")
    return "\n".join(lines)


@mcp.tool()
def search_moocs(query: str, top_k: int = 5) -> str:
    """Search MOOC transcripts for content semantically relevant to the query.

    Returns the top matching transcript chunks with their source title and
    content. Use this for questions about Earth Observation concepts covered
    in educational courses.

    Args:
        query: Natural-language question or topic to search for.
        top_k: Number of results to return (default 5).

    Returns:
        Ranked transcript chunks with title and relevance score.
    """
    logger.info("search_moocs: query=%r top_k=%d", query, top_k)
    try:
        candidates = _retrieve(query, top_k=20)
        top = _rerank(query, candidates, top_k=top_k)
        return _format(top)
    except Exception as e:
        logger.error("search_moocs error: %s", e, exc_info=True)
        return f"Error during retrieval: {e}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ESA MOOCs RAG MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    _ensure_ingested()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
