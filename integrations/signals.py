"""Platform events for webhook connectors (Zapier, Make)."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="core.File")
def document_uploaded(sender, instance, created, **kwargs):
    if not created or not instance.user_id:
        return
    from .models import ConnectorAccount
    if not ConnectorAccount.objects.filter(user_id=instance.user_id, service__in=("zapier", "make")).exists():
        return

    def send():
        from .connectors import notify
        notify(instance.user, "document.uploaded", {
            "file_id": instance.id, "filename": instance.filename, "matter": instance.project_id,
            "service": instance.service_id, "size": instance.file_size, "type": instance.file_type})
    transaction.on_commit(send)
