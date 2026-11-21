from django.db import models
from django.utils import timezone


class FilingHint(models.Model):
    """Where an organisation files email from a sender's domain (ranks matter suggestions in Outlook)."""
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="filing_hints")
    sender_domain = models.CharField(max_length=253)
    matter = models.ForeignKey("core.Matter", on_delete=models.CASCADE, related_name="filing_hints")
    count = models.PositiveIntegerField(default=0)
    last_used = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["client", "sender_domain", "matter"], name="uniq_filing_hint")]
        indexes = [models.Index(fields=["client", "sender_domain"])]
