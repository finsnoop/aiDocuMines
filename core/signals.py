import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from core.models import File
from core.elastic_indexes import FileIndex

logger = logging.getLogger(__name__)


def _log_file_activity(instance, event, metadata=None):
    """Log file-related activity for the owning user."""
    try:
        from custom_authentication.utils import log_activity
        log_activity(instance.user, event, metadata or {
            "file_id": instance.id,
            "filename": instance.filename,
            "project_id": instance.project_id,
            "service_id": instance.service_id,
        })
    except Exception:
        pass


@receiver(post_save, sender=File)
def index_file_to_elasticsearch(sender, instance, created, **kwargs):
    if created:
        _log_file_activity(instance, "FILE_UPLOADED")
    from document_search.utils import extract_text
    client_id = None
    if instance.user and instance.user.client:
        client_id = str(instance.user.client.id)

    content_text = extract_text(instance.filepath) if instance.filepath else ""

    doc = FileIndex(
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
        user_id=instance.user.id if instance.user else None,
        client_id=client_id,
        content=content_text,
    )
    doc.save()
    logger.info("Indexed file %s -> Elasticsearch.", instance.id)


@receiver(post_delete, sender=File)
def delete_file_from_es_index(sender, instance, **kwargs):
    _log_file_activity(instance, "FILE_DELETED")
    try:
        doc = FileIndex.get(id=str(instance.id))
        doc.delete()
        logger.info("Deleted file %s from Elasticsearch index.", instance.id)
    except Exception:
        logger.debug("File %s not found in ES index; skipping delete.", instance.id)


@receiver(post_save, sender=File)
def index_file_to_milvus(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from document_search.tasks import index_file as milvus_index_task
        milvus_index_task.delay(instance.id, force=False)
        logger.info("Queued file %s for Milvus vector indexing.", instance.id)
    except Exception as e:
        logger.warning("Failed to queue Milvus indexing for file %s: %s", instance.id, e)
