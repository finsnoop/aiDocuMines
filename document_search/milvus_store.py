"""
One reliable way to reach the Milvus collection.

Why this exists (the failures it removes):
  * The collection used to be loaded and RELEASED around every index and search.
    Concurrent requests released each other's collection ("collection not loaded").
    Now it is loaded once and stays loaded; nothing in the request path releases it.
  * A connection made once per process went stale after Milvus restarted, so
    everything failed until the workers were restarted. Calls now reconnect and
    retry once on connection errors.
  * Partition-per-user hits Milvus' partition limit as users grow; when a partition
    cannot be created the default partition is used (isolation is enforced by the
    file_id / client_id filter on every search, not by partitions).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import warnings
from functools import wraps

# Newer pymilvus releases flag the ORM API (connections/Collection) as deprecated
# ahead of its removal in 3.1. It is fully supported by the pinned 2.6.x client and
# by Milvus 2.6, so the per-call warnings are noise in the logs.
warnings.filterwarnings("ignore", message=r".*ORM-style PyMilvus API.*")

from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility  # noqa: E402

from document_search import config

LOGGER = logging.getLogger(__name__)
ALIAS = "default"
DEFAULT_PARTITION = "_default"
_lock = threading.Lock()
_loaded = set()


def chunk_hash(text: str) -> int:
    """Stable signed 64-bit fingerprint (Python's hash() changes on every restart)."""
    return int.from_bytes(hashlib.blake2b((text or "").encode("utf-8"), digest_size=8).digest(), "big", signed=True)


def _connect(force: bool = False) -> None:
    if force:
        try:
            connections.disconnect(ALIAS)
        except Exception:
            pass
        _loaded.clear()
    if force or not connections.has_connection(ALIAS):
        connections.connect(alias=ALIAS, host=config.MILVUS_HOST, port=str(config.MILVUS_PORT), timeout=10)


def _is_connection_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("connect", "unavailable", "channel", "not ready", "timeout", "closed", "deadline"))


def with_reconnect(fn):
    """Retry once with a fresh connection when Milvus was restarted or the channel died."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            LOGGER.warning("Milvus call failed (%s); reconnecting and retrying once.", exc)
            _connect(force=True)
            return fn(*args, **kwargs)
    return wrapper


def _index_params() -> dict:
    t, metric = config.MILVUS_INDEX_TYPE, config.MILVUS_METRIC_TYPE
    if t == "HNSW":
        return {"index_type": "HNSW", "metric_type": metric,
                "params": {"M": config.MILVUS_M, "efConstruction": config.MILVUS_EF_CONSTRUCTION}}
    if t == "DISKANN":
        return {"index_type": "DISKANN", "metric_type": metric}
    return {"index_type": t if t in ("IVF_SQ8", "IVF_FLAT") else "IVF_FLAT", "metric_type": metric,
            "params": {"nlist": config.MILVUS_NLIST}}


def search_params(limit: int = 10) -> dict:
    # HNSW rejects searches where ef < limit, so ef always covers the requested results.
    return {"metric_type": config.MILVUS_METRIC_TYPE,
            "params": {"nprobe": config.MILVUS_NPROBE, "ef": min(max(config.MILVUS_EF, int(limit)), 32768)}}


def _create_collection() -> Collection:
    schema = CollectionSchema(
        [
            FieldSchema("pk", DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema("file_id", DataType.INT64),
            FieldSchema("client_id", DataType.INT64),
            FieldSchema("chunk_hash", DataType.INT64),
            FieldSchema("source", DataType.VARCHAR, max_length=256),
            FieldSchema("chunk_text", DataType.VARCHAR, max_length=config.MAX_CHUNK_TEXT_LENGTH),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=config.VECTOR_DIM),
        ],
        description="Chunked document embeddings (multi-tenant)",
    )
    coll = Collection(name=config.COLLECTION_NAME, schema=schema, using=ALIAS)
    coll.create_index(field_name="vector", index_params=_index_params())
    coll.create_index(field_name="file_id", index_params={"index_type": "STL_SORT"})
    coll.create_index(field_name="client_id", index_params={"index_type": "STL_SORT"})
    LOGGER.info("Created Milvus collection %s (%s).", config.COLLECTION_NAME, config.MILVUS_INDEX_TYPE)
    return coll


@with_reconnect
def get_collection() -> Collection:
    """The collection, created if missing and loaded into memory (stays loaded)."""
    _connect()
    name = config.COLLECTION_NAME
    with _lock:
        if not utility.has_collection(name, using=ALIAS):
            try:
                _create_collection()
            except Exception as exc:  # another worker created it first
                if not utility.has_collection(name, using=ALIAS):
                    raise
                LOGGER.info("Collection created concurrently: %s", exc)
        coll = Collection(name, using=ALIAS)
        if "client_id" not in {f.name for f in coll.schema.fields}:
            raise RuntimeError(
                f"Milvus collection '{name}' has an old schema without client_id. "
                "Run: python manage.py repair_search_indexes --rebuild-milvus"
            )
        if name not in _loaded:
            state = str(utility.load_state(name, using=ALIAS))
            if "Loaded" not in state or "NotLoad" in state:
                coll.load()
            _loaded.add(name)
    return coll


def partition_for(coll: Collection, user_id: int) -> str:
    """Per-user partition when possible; the default partition when Milvus refuses more."""
    name = f"{config.PARTITION_PREFIX}{user_id}"
    try:
        if not coll.has_partition(name):
            coll.create_partition(name)
            coll.load(partition_names=[name])
        return name
    except Exception as exc:
        LOGGER.warning("Using default partition for user %s (could not create %s: %s)", user_id, name, exc)
        return DEFAULT_PARTITION


@with_reconnect
def replace_file_vectors(file_id: int, user_id: int, client_id: int, filename: str,
                         chunks: list[str], vectors: list) -> int:
    """Idempotently store a file's vectors: delete what exists for the file, then insert. Raises on failure."""
    coll = get_collection()
    coll.delete(f"file_id == {int(file_id)}")
    part = partition_for(coll, user_id)
    seen, rows = set(), []
    for text, vec in zip(chunks, vectors):
        h = chunk_hash(text)
        if h in seen:
            continue
        seen.add(h)
        rows.append((int(file_id), int(client_id), h, (filename or "")[:256],
                     (text or "")[: config.MAX_CHUNK_TEXT_LENGTH], list(map(float, vec))))
    batch = config.MILVUS_INSERT_BATCH
    for i in range(0, len(rows), batch):
        part_rows = rows[i: i + batch]
        coll.insert([[r[k] for r in part_rows] for k in range(6)], partition_name=part)
    return len(rows)


@with_reconnect
def delete_file_vectors(file_id: int) -> None:
    get_collection().delete(f"file_id == {int(file_id)}")


@with_reconnect
def indexed_file_ids(file_ids: list[int]) -> set:
    """Which of these files have at least one vector in Milvus."""
    coll, found = get_collection(), set()
    for i in range(0, len(file_ids), 50):  # 50 files x chunks stays under the query row limit
        part = [int(x) for x in file_ids[i: i + 50]]
        rows = coll.query(expr=f"file_id in {part}", output_fields=["file_id"], limit=16384)
        found |= {int(r["file_id"]) for r in rows}
    return found


MAX_IDS_IN_EXPR = 2000


@with_reconnect
def search(vector, accessible_ids: set, client_id: int, top_k: int) -> list:
    """
    Nearest chunks among the files the caller may read. Large access lists are
    filtered by organisation in Milvus and by file id afterwards, because very long
    `file_id in [...]` expressions are rejected by Milvus.
    """
    coll = get_collection()
    ids = sorted(int(i) for i in accessible_ids)
    if len(ids) <= MAX_IDS_IN_EXPR:
        expr, limit = f"file_id in {ids} and client_id == {int(client_id)}", top_k
    else:
        expr, limit = f"client_id == {int(client_id)}", min(max(top_k * 10, 100), 16384)
    res = coll.search(data=[list(map(float, vector))], anns_field="vector", param=search_params(limit),
                      limit=limit, expr=expr, output_fields=["file_id", "chunk_text"])
    allowed = set(ids)
    return [h for h in res[0] if int(h.entity.get("file_id")) in allowed]


@with_reconnect
def stats() -> dict:
    coll = get_collection()
    return {"collection": config.COLLECTION_NAME, "entities": coll.num_entities,
            "partitions": len(coll.partitions), "load_state": str(utility.load_state(config.COLLECTION_NAME, using=ALIAS))}
