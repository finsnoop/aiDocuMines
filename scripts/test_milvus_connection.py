#!/usr/bin/env python
"""
Quick Milvus round-trip health-check.

• Connects to the Milvus service defined in docker-compose (`host = "milvus"`).
• Creates a temporary collection.
• Inserts & indexes five demo vectors.
• Executes a similarity search.
• Drops the collection.
• Exits with 0 on success, 1 on failure.

Run with:
    docker compose exec web python scripts/test_milvus_connection.py
"""

from __future__ import annotations
import os
import sys
import logging
import traceback
from datetime import datetime
from typing import List

from pymilvus import (
    connections,
    utility,
    Collection,
    CollectionSchema,
    FieldSchema,
    DataType,
)
from sentence_transformers import SentenceTransformer

# ───────────────────────── Config ──────────────────────────
MILVUS_HOST = os.getenv("MILVUS_HOST", "milvus")      # service name in docker-compose
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLL_NAME   = "milvus_healthcheck"
MODEL_NAME  = "all-MiniLM-L6-v2"
VECTOR_DIM  = 384

SAMPLE_TEXTS = [
    "Milvus is a cloud-native vector database.",
    "This is only a test vector.",
    "Document-search feature health check.",
    "High-throughput similarity search demo.",
    "End-to-end embedding & query cycle.",
]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger("milvus-health")

# ───────────────────────── Helpers ─────────────────────────
def connect() -> None:
    """Connect (or reuse) the default Milvus alias."""
    if not connections.has_connection("default"):
        LOG.info("Connecting to Milvus at %s:%s …", MILVUS_HOST, MILVUS_PORT)
        connections.connect(alias="default", host=MILVUS_HOST, port=MILVUS_PORT)
    LOG.info("Connection established")

def prepare_collection() -> Collection:
    """Create / replace a temporary collection."""
    if utility.has_collection(COLL_NAME):
        LOG.warning("Old '%s' collection found – dropping it first", COLL_NAME)
        Collection(COLL_NAME).drop()

    schema = CollectionSchema(
        fields=[
            FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema("content", DataType.VARCHAR, max_length=1000),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        ],
        description="Temporary health-check collection",
    )
    coll = Collection(name=COLL_NAME, schema=schema)
    LOG.info("Collection '%s' created", COLL_NAME)
    return coll

def embed_texts(texts: List[str]) -> List[List[float]]:
    LOG.info("Encoding %d sample sentences with %s …", len(texts), MODEL_NAME)
    model = SentenceTransformer(MODEL_NAME)
    return model.encode(texts).tolist()

def run_roundtrip() -> None:
    """Full cycle: connect → create → insert → search → drop."""
    connect()
    coll = prepare_collection()

    # Insert
    vectors = embed_texts(SAMPLE_TEXTS)
    coll.insert([SAMPLE_TEXTS, vectors])
    coll.flush()

    # Index & load
    coll.create_index(
        field_name="vector",
        index_params={
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
            "params": {"nlist": 64},
        },
    )
    coll.load()

    # Search (use first sentence as the query)
    query_vec = [vectors[0]]
    results = coll.search(
        data=query_vec,
        anns_field="vector",
        param={"metric_type": "COSINE", "params": {"nprobe": 10}},
        limit=3,
        output_fields=["content"],
    )

    LOG.info("Top-3 search results:")
    for hit in results[0]:
        LOG.info("   • score %.4f  →  \"%s…\"", hit.score, hit.entity.get("content")[:60])

    # Drop
    coll.drop()
    LOG.info("Temporary collection dropped – health-check successful")

# ───────────────────────── Main ────────────────────────────
if __name__ == "__main__":
    LOG.info("Milvus health-check started @ %s UTC", datetime.utcnow().isoformat(timespec="seconds"))
    try:
        run_roundtrip()
        LOG.info("Milvus is UP and fully operational.")
        sys.exit(0)
    except Exception as exc:                 # pragma: no cover
        LOG.error("Milvus health-check FAILED\n%s", traceback.format_exc())
        sys.exit(1)

