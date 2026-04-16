#!/usr/bin/env python3
"""
Local test script for the EVE Retrieval MCP Server.

Calls the retrieve tool directly (async import from server.py) to verify
authentication and the full retrieval pipeline against the EVE staging API.

Credentials are loaded from the .env file in this directory, or can be
overridden via environment variables (EVE_EMAIL, EVE_PASSWORD).

Usage:
    python test.py                                  # default query
    python test.py --query "What is Copernicus?"    # custom query
    python test.py --query "Sentinel-2 bands" --k 5 # more documents
    python test.py --query "climate change" --collections "Wikipedia EO"
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def test_retrieve(
    query: str,
    k: int,
    score_threshold: float,
    public_collections: list[str] | None,
):
    """Call the retrieve tool and print results."""
    from server import retrieve

    print("=" * 60)
    print("TEST: retrieve")
    print(f"  query:              {query}")
    print(f"  k:                  {k}")
    print(f"  score_threshold:    {score_threshold}")
    print(f"  public_collections: {public_collections or '(server defaults)'}")
    print("=" * 60)

    t0 = time.time()
    result_json = await retrieve(
        query=query,
        k=k,
        score_threshold=score_threshold,
        public_collections=public_collections,
    )
    elapsed = time.time() - t0

    result = json.loads(result_json)

    if "error" in result:
        print(f"\nError: {result['error']}")
        print(f"Detail: {result.get('detail', '')[:500]}")
        return result

    docs = result.get("retrieved_docs", [])
    latencies = result.get("latencies", {})

    print(f"\nDone in {elapsed:.1f}s")
    print(f"  original_query: {result.get('original_query', '?')}")
    print(f"  requery:        {result.get('requery', '?')}")
    print(f"  documents:      {len(docs)}")

    if latencies:
        print("\n  Latencies:")
        for key, val in latencies.items():
            if val is not None:
                print(f"    {key}: {val}")

    if docs:
        print("\n  Retrieved documents:")
        for i, doc in enumerate(docs):
            score = doc.get("score")
            rerank = doc.get("reranking_score")
            coll = doc.get("collection_name", "?")
            text = doc.get("text", "")
            meta = doc.get("metadata", {})
            title = meta.get("title", meta.get("name", ""))

            score_str = f"score={score:.4f}" if score is not None else ""
            rerank_str = f" rerank={rerank:.4f}" if rerank is not None else ""

            print(f"\n  [{i+1}] {coll} | {score_str}{rerank_str}")
            if title:
                print(f"      title: {title}")
            preview = text[:200].replace("\n", " ")
            if preview:
                print(f"      text:  {preview}...")
    else:
        print("\n  No documents returned.")

    return result


async def test_login():
    """Verify that login works and a token is obtained."""
    from server import _login

    print("=" * 60)
    print("TEST: login")
    print("=" * 60)

    t0 = time.time()
    token = await _login()
    elapsed = time.time() - t0

    print(f"\nDone in {elapsed:.1f}s")
    print(f"  token: {token[:20]}...{token[-10:]}")
    print(f"  length: {len(token)} chars")
    return token


def main():
    parser = argparse.ArgumentParser(
        description="Test EVE Retrieval MCP Server locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test.py
  python test.py --query "What is Copernicus?"
  python test.py --query "Sentinel-2 bands" --k 5
  python test.py --query "climate change" --collections "Wikipedia EO" "qwen-512-filtered"
  python test.py --login-only
        """,
    )
    parser.add_argument("--query", "-q", default="What is ESA?",
                        help="Search query (default: 'What is ESA?')")
    parser.add_argument("--k", "-k", type=int, default=3,
                        help="Number of documents to retrieve (default: 3)")
    parser.add_argument("--score-threshold", "-s", type=float, default=0.7,
                        help="Minimum similarity score (default: 0.7)")
    parser.add_argument("--collections", "-c", nargs="+", default=None,
                        help="Public collection names (default: server defaults)")
    parser.add_argument("--login-only", action="store_true",
                        help="Only test login, skip retrieval")

    args = parser.parse_args()

    if args.login_only:
        asyncio.run(test_login())
        return

    asyncio.run(test_login())
    print()
    asyncio.run(test_retrieve(
        query=args.query,
        k=args.k,
        score_threshold=args.score_threshold,
        public_collections=args.collections,
    ))


if __name__ == "__main__":
    main()
