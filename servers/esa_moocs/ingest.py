"""
ingest.py
=========
Loads MOOC transcript chunks from data/MOOC.jsonl into a local Qdrant
collection. Run once; subsequent server starts skip this step.

Usage:
    python ingest.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

COLLECTION = "esa_moocs"
EMBED_MODEL = "nasa-impact/nasa-smd-ibm-st-v2"
VECTOR_DIM = 768


def run(server_dir: Path) -> None:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
    from sentence_transformers import SentenceTransformer

    data_path = server_dir / "data" / "MOOC.jsonl"
    storage_path = server_dir / "qdrant_storage"

    logger.info("Loading embedding model %s", EMBED_MODEL)
    embedder = SentenceTransformer(EMBED_MODEL)

    logger.info("Reading %s", data_path)
    records = []
    with data_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    logger.info("Loaded %d chunks", len(records))

    client = QdrantClient(path=str(storage_path))

    client.recreate_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )

    texts = [r["content"] for r in records]
    logger.info("Embedding %d chunks (this may take a minute)...", len(texts))
    vectors = embedder.encode(texts, show_progress_bar=True, batch_size=32)

    points = [
        PointStruct(
            id=i,
            vector=vectors[i].tolist(),
            payload={
                "content": r["content"],
                "title": r["metadata"].get("video_title", ""),
                "language": r["metadata"].get("language", ""),
                "video_id": r["metadata"].get("video_id", ""),
                "duration": r["metadata"].get("duration", 0.0),
            },
        )
        for i, r in enumerate(records)
    ]

    client.upsert(collection_name=COLLECTION, points=points)
    logger.info("Ingested %d points into collection '%s'", len(points), COLLECTION)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run(Path(__file__).resolve().parent)
