# document_search/milvus_client.py
"""
Robust Milvus connection management.

Problem: pymilvus `connections.has_connection("default")` returns True
even when the TCP socket is dead (e.g. after Milvus restart).
Solution: Always try a lightweight probe; reconnect on failure.
"""
import logging
from pymilvus import connections, Collection, utility
from .config import MILVUS_HOST, MILVUS_PORT, COLLECTION_NAME

logger = logging.getLogger(__name__)

_ALIAS = "default"


def ensure_milvus_connection() -> None:
    """Verify the Milvus connection is alive; reconnect silently if dead."""
    # Fast path: no registered connection at all
    if not connections.has_connection(_ALIAS):
        _connect()
        return

    # Probe: try to list collections (lightweight gRPC call)
    try:
        utility.list_collections()
    except Exception:
        logger.warning("Milvus connection stale — reconnecting")
        try:
            connections.disconnect(_ALIAS)
        except Exception:
            pass
        _connect()


def _connect() -> None:
    """Establish a fresh connection."""
    connections.connect(alias=_ALIAS, host=MILVUS_HOST, port=MILVUS_PORT)
    logger.info("Milvus connected to %s:%s", MILVUS_HOST, MILVUS_PORT)


def get_milvus_collection() -> Collection:
    """Get the collection with a verified connection."""
    ensure_milvus_connection()
    return Collection(COLLECTION_NAME)
