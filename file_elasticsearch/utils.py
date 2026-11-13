import logging
from elasticsearch_dsl import Q, Search
from elasticsearch.helpers import streaming_bulk
from django.conf import settings
from core.elastic_indexes import FileIndex
from document_operations.utils import get_user_accessible_file_ids

logger = logging.getLogger(__name__)

try:
    from document_search import config
    ES_BATCH_SIZE = getattr(config, "ES_BATCH_SIZE", 500)
    ES_SEARCH_PAGINATE_BY = getattr(config, "ES_SEARCH_PAGINATE_BY", 50)
except ImportError:
    ES_BATCH_SIZE = 500
    ES_SEARCH_PAGINATE_BY = 50


def delete_index():
    FileIndex._index.delete(ignore=404)


def create_index():
    FileIndex.init()


def _build_doc(file_instance):
    """Build a FileIndex document dict from a File model instance."""
    from document_search.utils import extract_text
    client_id = None
    if file_instance.user and file_instance.user.client:
        client_id = str(file_instance.user.client.id)
    elif hasattr(file_instance, "client_id") and file_instance.client_id:
        client_id = str(file_instance.client_id)
    content_text = extract_text(file_instance.filepath) if file_instance.filepath else ""
    return {
        "_id": str(file_instance.id),
        "_index": "files",
        "id": str(file_instance.id),
        "filename": file_instance.filename,
        "filepath": file_instance.filepath,
        "file_size": file_instance.file_size,
        "status": file_instance.status,
        "project_id": file_instance.project_id,
        "service_id": file_instance.service_id,
        "created_at": file_instance.created_at,
        "updated_at": file_instance.updated_at,
        "md5_hash": file_instance.md5_hash,
        "user_id": file_instance.user_id if file_instance.user_id else None,
        "client_id": client_id,
        "content": content_text,
    }


def index_file(file_instance):
    doc = _build_doc(file_instance)
    FileIndex(meta={"id": doc["_id"]}, **{k: v for k, v in doc.items() if k != "_id"}).save()


def bulk_index_files(file_iter, batch_size=ES_BATCH_SIZE):
    """Bulk-index file instances using ES streaming_bulk for speed."""
    from elasticsearch_dsl.connections import connections as es_connections
    client = es_connections.get_connection()
    success = 0
    for ok, result in streaming_bulk(client, (_build_doc(f) for f in file_iter), chunk_size=batch_size):
        if ok:
            success += 1
    return success


def force_reindex():
    from core.models import File
    delete_index()
    create_index()
    total = File.objects.count()
    logger.info("Starting bulk re-index of %s files ...", total)
    qs = File.objects.all().iterator(chunk_size=ES_BATCH_SIZE)
    indexed = bulk_index_files(qs)
    logger.info("Re-indexed %s / %s files.", indexed, total)


def _tenant_q(user, accessible_ids=None):
    """Tenancy filter: (user_id == me) OR (doc _id in my accessible file ids)."""
    if accessible_ids is None:
        accessible_ids = get_user_accessible_file_ids(user)
    id_values = [str(i) for i in accessible_ids]
    shoulds = [Q("term", user_id=user.id)]
    if id_values:
        shoulds.append(Q("ids", values=id_values))
    return Q("bool", should=shoulds, minimum_should_match=1)


def basic_search(query, scope="both", user=None, accessible_ids=None, page=1, page_size=None):
    """Full-text search with pagination and relevance ranking."""
    if not query:
        return {"results": [], "total": 0, "page": page, "page_size": page_size}
    if page_size is None:
        page_size = ES_SEARCH_PAGINATE_BY

    s = FileIndex.search()

    if user is not None:
        s = s.filter(_tenant_q(user, accessible_ids))

    # Build ranked query
    if scope == "filename":
        s = s.query(
            Q("multi_match", query=query, fields=["filename^3", "filepath^2", "filename.trigram^2"], type="best_fields")
        )
    elif scope == "content":
        s = s.query(
            Q("match", content={"query": query, "minimum_should_match": "70%"})
        )
    else:
        # Rank: filename matches score highest, then filepath, then content
        s = s.query(
            Q("bool",
              should=[
                  Q("multi_match", query=query, fields=["filename^4", "filename.trigram^3"], type="best_fields", boost=3),
                  Q("multi_match", query=query, fields=["filepath^2", "filepath.trigram^2"], type="best_fields", boost=2),
                  Q("match", content={"query": query, "minimum_should_match": "60%", "boost": 1}),
              ],
              minimum_should_match=1,
            )
        )

    # Pagination
    start = (page - 1) * page_size
    s = s[start:start + page_size]

    # Execute
    response = s.execute()

    results = []
    for hit in response:
        d = hit.to_dict()
        if hasattr(hit.meta, "score") and hit.meta.score is not None:
            d["_score"] = hit.meta.score
        results.append(d)
    return {
        "results": results,
        "total": response.hits.total.value if hasattr(response.hits.total, "value") else len(results),
        "page": page,
        "page_size": page_size,
        "max_score": response.hits.max_score,
    }


def advanced_search(must=None, filter_clauses=None, search_in=None, user=None, accessible_ids=None, page=1, page_size=None):
    """Advanced search with field-specific filters and pagination."""
    must = must or []
    filter_clauses = filter_clauses or []
    if page_size is None:
        page_size = ES_SEARCH_PAGINATE_BY

    must_clauses = []
    for clause in must:
        field = clause.get("field")
        value = clause.get("value")
        if field == "content":
            must_clauses.append({"match": {"content": {"query": value, "minimum_should_match": "70%"}}})
        elif field in ("filename", "filepath"):
            must_clauses.append({
                "multi_match": {
                    "query": value,
                    "fields": [f"{field}^3", f"{field}.trigram^2"],
                    "type": "best_fields",
                }
            })
        else:
            must_clauses.append({"wildcard": {field: {"value": value, "case_insensitive": True}}})

    filter_es = [{"term": {f["field"]: f["value"]}} for f in filter_clauses]

    s = FileIndex.search().update_from_dict({
        "query": {
            "bool": {
                "must": must_clauses if must_clauses else [{"match_all": {}}],
                "filter": filter_es,
            }
        }
    })

    if user is not None:
        s = s.filter(_tenant_q(user, accessible_ids))

    # Pagination
    start = (page - 1) * page_size
    s = s[start:start + page_size]

    response = s.execute()
    results = []
    for hit in response:
        d = hit.to_dict()
        if hasattr(hit.meta, "score") and hit.meta.score is not None:
            d["_score"] = hit.meta.score
        results.append(d)

    return {
        "results": results,
        "total": response.hits.total.value if hasattr(response.hits.total, "value") else len(results),
        "page": page,
        "page_size": page_size,
        "max_score": response.hits.max_score,
    }
