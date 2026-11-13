# file_elasticsearch/tasks.py

from celery import shared_task
from .utils import force_reindex

@shared_task
def reindex_files_task():
    force_reindex()
    return "Reindex completed"



@shared_task(
    name="file_elasticsearch.index_file",
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=1800,
    retry_jitter=True,
    max_retries=6,
)
def es_index_file(file_id: int) -> str:
    """Index one file into Elasticsearch (retried with backoff while Elasticsearch is unavailable)."""
    from core.models import File
    from core.signals import _index_file_to_elasticsearch

    f = File.objects.select_related("user", "user__client").filter(pk=file_id).first()
    if f is None:
        return "missing"
    _index_file_to_elasticsearch(f)
    return "ok"


@shared_task(
    name="file_elasticsearch.delete_file",
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=1800,
    max_retries=6,
)
def es_delete_file(file_id: int) -> str:
    from elasticsearch import NotFoundError
    from core.elastic_indexes import FileIndex

    try:
        FileIndex.get(id=str(file_id)).delete()
    except NotFoundError:
        return "absent"
    return "deleted"
