import os

# ── Milvus ──────────────────────────────────────────────────
MILVUS_HOST = os.getenv("MILVUS_HOST", "milvus")
MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530"))
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION", "doc_embeddings_v2")
PARTITION_PREFIX = os.getenv("MILVUS_PARTITION_PREFIX", "user_")

# Index type: IVF_FLAT, IVF_SQ8, HNSW, DISKANN
# HNSW is recommended for billion-scale (>10M vectors) with high recall
MILVUS_INDEX_TYPE = os.getenv("MILVUS_INDEX_TYPE", "HNSW")
MILVUS_METRIC_TYPE = os.getenv("MILVUS_METRIC_TYPE", "COSINE")
MILVUS_NLIST = int(os.getenv("MILVUS_NLIST", "256"))
MILVUS_NPROBE = int(os.getenv("MILVUS_NPROBE", "16"))
# HNSW specific params
MILVUS_M = int(os.getenv("MILVUS_M", "16"))
MILVUS_EF_CONSTRUCTION = int(os.getenv("MILVUS_EF_CONSTRUCTION", "200"))
MILVUS_EF = int(os.getenv("MILVUS_EF", "64"))
# IVF SQ8 specific
MILVUS_NCENTROIDS = int(os.getenv("MILVUS_NCENTROIDS", "16384"))

# Batch sizes
MILVUS_INSERT_BATCH = int(os.getenv("MILVUS_INSERT_BATCH", "500"))
MILVUS_SEARCH_TOP_K = int(os.getenv("MILVUS_SEARCH_TOP_K", "100"))

# Chunking
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "64"))
MAX_CHUNK_TEXT_LENGTH = int(os.getenv("MAX_CHUNK_TEXT_LENGTH", "5000"))

# Embedding
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "384"))

# Elasticsearch
# One node: 1 shard, 0 replicas (a replica can never be placed, so the index stays "yellow").
ES_SHARDS = int(os.getenv("ES_SHARDS", "1"))
ES_REPLICAS = int(os.getenv("ES_REPLICAS", "0"))
ES_BATCH_SIZE = int(os.getenv("ES_BATCH_SIZE", "500"))
ES_SEARCH_PAGINATE_BY = int(os.getenv("ES_SEARCH_PAGINATE_BY", "50"))
