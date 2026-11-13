from django.db import models
from core.models import File
import uuid

class AnonymizationRun(models.Model):
    """
    Tracks each anonymization request.
    """
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Processing', 'Processing'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]

    ANONYMIZATION_TYPE_CHOICES = [
        ('Presidio', 'Presidio-based Anonymization'),
        ('Spacy', 'Spacy-based Anonymization')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, null=True, blank=True, related_name='anonymization_runs')
    client_name = models.CharField(max_length=255, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending', db_index=True)
    anonymization_type = models.CharField(max_length=20, choices=ANONYMIZATION_TYPE_CHOICES, default='Presidio', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    error_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"AnonymizationRun {self.id} - {self.status} ({self.anonymization_type})"


class Anonymize(models.Model):
    """
    Stores details of anonymized documents including both Presidio & Spacy pipeline outputs.
    """
    STATUS_CHOICES = [
        ('Processing', 'Processing'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]

    FILE_TYPE_CHOICES = [
        ('structured', 'Structured'),
        ('plain', 'Plain')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AnonymizationRun, on_delete=models.CASCADE, related_name="anonymized_files", null=True, blank=True)
    original_file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="anonymized_versions")
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES, default='plain', db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    anonymized_filepath = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_html_filepath = models.CharField(max_length=1024, blank=True, null=True)
    entity_mapping_filepath = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_markdown_filepath = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_structured_filepath = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_structured_txt_filepath = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_structured_html_filepath = models.CharField(max_length=1024, blank=True, null=True)

    presidio_masking_map = models.JSONField(blank=True, null=True)
    spacy_masking_map = models.JSONField(blank=True, null=True)

    risk_score = models.FloatField(blank=True, null=True)
    risk_level = models.CharField(max_length=20, blank=True, null=True)
    risk_breakdown = models.JSONField(blank=True, null=True)
    risk_contributions = models.JSONField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Processing', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Anonymized File {self.original_file.filename} - {self.status}"
    
    class Meta:
        constraints = [
                models.UniqueConstraint(fields=["original_file", "file_type"], condition=models.Q(is_active=True), name="unique_active_anonymize_per_file_type")
                ]


class DeAnonymize(models.Model):
    """
    Stores results of reversing anonymization using Presidio + Spacy masking maps.
    """
    STATUS_CHOICES = [
        ('Processing', 'Processing'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="deanonymized_versions")
    unmasked_text = models.TextField(blank=True, null=True)
    unmasked_filepath = models.CharField(max_length=1024, blank=True, null=True)
    presidio_masking_map = models.JSONField(blank=True, null=True)
    spacy_masking_map = models.JSONField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Processing', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DeAnonymized File {self.file.filename} - {self.status}"


class AnonymizationStorage(models.Model):
    """
    Stores file paths for upload, anonymized, and de-anonymized stages (optional, currently unused).
    """
    storage_id = models.AutoField(primary_key=True)
    run = models.ForeignKey(AnonymizationRun, on_delete=models.CASCADE, related_name="storages", null=True, blank=True)
    upload_storage_location = models.CharField(max_length=1024, blank=True, null=True)
    anonymized_storage_location = models.CharField(max_length=1024, blank=True, null=True)
    deanonymized_storage_location = models.CharField(max_length=1024, blank=True, null=True)

    def __str__(self):
        return f"Storage {self.storage_id} - {self.upload_storage_location}"



class AnonymizationStats(models.Model):
    """
    Stores historical snapshots of anonymization statistics.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Filters
    client_name = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    project_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    service_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    # Computed statistics
    files_with_entities = models.PositiveIntegerField(default=0)
    files_without_entities = models.PositiveIntegerField(default=0)
    total_entities_anonymized = models.PositiveIntegerField(default=0)
    entity_type_breakdown = models.JSONField(default=dict)

    def __str__(self):
        return f"AnonymizationStats {self.id} ({self.created_at.date()})"

