from django.db import models
from core.models import File  # Reference to original File model from core
import uuid


class TranslationRun(models.Model):
    """Tracks each translation request."""
    
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Translating', 'Translating'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, null=True, blank=True, related_name='translation_runs')
    client_name = models.CharField(max_length=255, db_index=True)  # Include client_name for tracking
    from_language = models.CharField(max_length=10, db_index=True)
    to_language = models.CharField(max_length=10, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    error_message = models.TextField(blank=True, null=True)  # Store errors if translation fails

    def __str__(self):
        return f"TranslationRun {self.id} - {self.status} ({self.from_language} → {self.to_language})"


class TranslationFile(models.Model):
    """
    Stores details of both the original and translated files.
    - Links to `File` from `core.models` (original file)
    - Stores a separate `translated_filepath`
    """
    
    STATUS_CHOICES = [
        ('Processing', 'Processing'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # Unique file ID
    run = models.ForeignKey(
        TranslationRun, on_delete=models.CASCADE, related_name="translation_files", null=True, blank=True
    )  # Run linked to this translation
    original_file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="translated_versions")  # Link to the original file
    original_filepath = models.CharField(max_length=1024, blank=True, null=True)  # Store original file path
    translated_filepath = models.CharField(max_length=1024, blank=True, null=True)  # Store translated file path
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Processing', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("original_file", "run")  # Prevents duplicate translations per language

    def __str__(self):
        return f"Translated File {self.original_file.filename} - {self.status} ({self.run.from_language} → {self.run.to_language})"


class TranslationLanguage(models.Model):
    """
    Represents supported languages for translation.
    """
    
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True, db_index=True)
    code = models.CharField(max_length=20, unique=True, db_index=True)

    def __str__(self):
        return f"{self.name} ({self.code})"


class TranslationStorage(models.Model):
    """
    Represents storage locations for uploaded and translated files.
    - Supports multiple versions of translated files.
    """
    
    storage_id = models.AutoField(primary_key=True)
    run = models.ForeignKey(
        TranslationRun, on_delete=models.CASCADE, related_name="translation_storages", null=True, blank=True
    )  # Ensures storage links to a run
    upload_storage_location = models.CharField(max_length=1024, blank=True, null=True)  # Path for uploaded file
    translated_storage_location = models.CharField(max_length=1024, blank=True, null=True)  # Path for translated file
    
    def __str__(self):
        return f"Storage {self.storage_id} - {self.upload_storage_location} → {self.translated_storage_location}"
