"""
Keep the search indexes in step with files.

Indexing never runs inside the web request and never blocks a save or delete:
work is queued after the database transaction commits (so workers always find
the row), and the tasks retry with backoff while Elasticsearch or Milvus is down.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from core.elastic_indexes import FileIndex
from core.models import File

logger = logging.getLogger(__name__)

# Saves that only touch these fields do not change what is searchable.
NON_SEARCH_FIELDS = {"document_type", "status", "updated_at", "content"}


def _queue(task, *args, label=""):
    def send():
        try:
            task.delay(*args)
        except Exception as exc:  # broker down: the nightly repair picks it up
            logger.warning("Could not queue %s for %s: %s", label or task.name, args, exc)
    transaction.on_commit(send)


@receiver(post_save, sender=File)
def index_file_to_elasticsearch(sender, instance, created=False, update_fields=None, **kwargs):
    if update_fields and set(update_fields) <= NON_SEARCH_FIELDS:
        return
    from file_elasticsearch.tasks import es_index_file
    _queue(es_index_file, instance.id, label="Elasticsearch indexing")


def _index_file_to_elasticsearch(instance):
    from document_search.utils import extract_text

    client_id = str(instance.user.client_id) if instance.user and instance.user.client_id else None
    content_text = extract_text(instance.filepath) if instance.filepath else ""
    FileIndex(
        meta={"id": str(instance.id)},
        id=str(instance.id),
        filename=instance.filename,
        filepath=instance.filepath,
        file_size=instance.file_size,
        status=instance.status,
        project_id=instance.project_id,
        service_id=instance.service_id,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        md5_hash=instance.md5_hash,
        user_id=instance.user_id,
        client_id=client_id,
        content=content_text,
    ).save()
    logger.info("Indexed file %s -> Elasticsearch.", instance.id)


@receiver(post_delete, sender=File)
def delete_file_from_es_index(sender, instance, **kwargs):
    from document_search.tasks import delete_file_vectors_task
    from file_elasticsearch.tasks import es_delete_file

    _queue(es_delete_file, instance.id, label="Elasticsearch delete")
    _queue(delete_file_vectors_task, instance.id, label="Milvus delete")


@receiver(post_save, sender=File)
def index_file_to_milvus(sender, instance, created, **kwargs):
    if not created:
        return
    from document_search.tasks import index_file as milvus_index_task

    _queue(milvus_index_task, instance.id, label="Milvus indexing")
