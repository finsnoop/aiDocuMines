"""
document_search.models
~~~~~~~~~~~~~~~~~~~~~

Django ORM models that complement the Milvus-backed vector index.

Key Entities
------------
VectorChunk
    • One row per embedded text chunk.
    • Links back to core.File for permissions / billing / analytics.
    • Stores the raw embedding so you can rebuild Milvus or move
      to a different vector DB without re-embedding.

SearchQueryLog
    • Optional audit trail of user search activity.
    • Helps with usage analytics, abuse detection, and debugging.
"""

from __future__ import annotations

from django.db import models
from django.contrib.auth import get_user_model

# The File model lives in the existing core app.
from core.models import File

from django.db.models.functions import Length

import hashlib

User = get_user_model()


# ─────────────────────────────── VectorChunk ────────────────────────────────
class VectorChunk(models.Model):
    """
    A single semantic chunk derived from an uploaded File.

    Milvus stores the actual vector for ANN search, but we keep a
    copy of the float list here (JSON) so we can:
      • rebuild / reseed the vector DB
      • perform local diagnostics without hitting Milvus
      • ensure deterministic re-indexing (no duplicate work)
    """
    id = models.BigAutoField(primary_key=True)

    # Links
    file = models.ForeignKey(
        File,
        on_delete=models.CASCADE,
        related_name="vector_chunks",
        help_text="Source document",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="vector_chunks",
        editable=False,
    )

    # Chunk metadata
    chunk_index = models.PositiveIntegerField(
        help_text="Position of chunk in original text (0-based)."
    )
    chunk_text = models.TextField()
    embedding = models.JSONField(help_text="Dense vector (list[float])")

    # House-keeping
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("file", "chunk_index")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["file"]),
        ]
        ordering = ["file_id", "chunk_index"]

    chunk_hash = models.CharField(
        max_length=64,
        unique=False,  # not globally unique, only dedup by file/user if needed
        db_index=True,
        help_text="SHA256 hash of chunk_text for deduplication.",
    )

    # --------------------------------------------------------------------- #
    # Helper properties
    # --------------------------------------------------------------------- #
    def save(self, *args, **kwargs):
        if not self.user_id:
            self.user_id = self.file.user_id
        if self.chunk_text and not self.chunk_hash:
            self.chunk_hash = hashlib.sha256(self.chunk_text.encode()).hexdigest()
        super().save(*args, **kwargs)

    #def save(self, *args, **kwargs):
    #    # Sync user from File automatically on first save
    #    if not self.user_id:
    #        self.user_id = self.file.user_id
    #    super().save(*args, **kwargs)

    @property
    def partition_name(self) -> str:
        """
        Partition label used in Milvus — mirrors tasks.py logic.
        """
        return f"user_{self.user_id}"

    def __str__(self) -> str:  # pragma: no cover
        return f"Chunk#{self.chunk_index} ({self.file.filename})"


# ───────────────────────────── SearchQueryLog ───────────────────────────────
class SearchQueryLog(models.Model):
    id = models.BigAutoField(primary_key=True)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="search_logs")
    file = models.ForeignKey(File, on_delete=models.SET_NULL, null=True, blank=True)
    
    query_text = models.TextField()
    top_k = models.PositiveIntegerField(default=5)

    duration_ms = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Server-measured latency for the query (ms).",
    )
    result_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Number of hits returned to the client.",
    )
    result_json = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Q<{self.id}> by {self.user} @ {self.created_at:%Y-%m-%d %H:%M}"


