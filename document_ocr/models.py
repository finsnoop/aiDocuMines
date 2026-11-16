from django.db import models
from django.contrib.auth import get_user_model
from core.models import File  # Reference to the original File model from core
import uuid

User = get_user_model()

class OCRRun(models.Model):
    """
    Tracks each OCR request.
    """
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Processing', 'Processing'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed')
    ]

    OCR_OPTION_CHOICES = [
        ('Basic-ocr', 'Basic OCR'),
        ('Advanced-ocr', 'Advanced OCR')
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, null=True, blank=True, related_name='ocr_runs')
    client_name = models.CharField(max_length=255, db_index=True)  # Include client_name for tracking
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending', db_index=True)
    ocr_option = models.CharField(max_length=20, choices=OCR_OPTION_CHOICES, default='Basic-ocr', db_index=True)  # Store OCR type
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    error_message = models.TextField(blank=True, null=True)  # Store errors if OCR fails

    def __str__(self):
        return f"OCRRun {self.id} - {self.status} ({self.ocr_option})"


class OCRFile(models.Model):
    """
    Stores details of both the original and OCR-processed files.
    - Links to `File` from `core.models` (original file)
    - Stores the OCR-processed file path
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # Ensure unique file ID
    run = models.ForeignKey(
        OCRRun, on_delete=models.SET_NULL, related_name="ocr_files", null=True, blank=True
    )  # Nullable because some OCR tasks may not be linked to a run directly
    original_file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="ocr_versions")  # Link to the original file
    ocr_filepath = models.CharField(max_length=1024, blank=True, null=True)  # Path to OCR-processed file
    raw_docx_path = models.CharField(max_length=1024, blank=True, null=True)  # Path to extracted plain DOCX file
    docx_path = models.CharField(max_length=1024, blank=True, null=True)  # Path to formatted DOCX file
    status = models.CharField(
        max_length=20, 
        choices=[('Processing', 'Processing'), ('Completed', 'Completed'), ('Failed', 'Failed')],
        default='Processing',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    ocr_option = models.CharField(max_length=20, choices=[('Basic-ocr', 'Basic OCR'), ('Advanced-ocr', 'Advanced OCR')], blank=True, null=True)

    def __str__(self):
        return f"OCR File {self.original_file.filename} - {self.status}"


    class Meta:
        unique_together = ("original_file", "ocr_option")  # ensures DB uniqueness



class OCRStorage(models.Model):
    """
    Represents storage locations for uploaded and OCR-processed files.
    """
    storage_id = models.AutoField(primary_key=True)
    run = models.ForeignKey(
        OCRRun, on_delete=models.SET_NULL, related_name="storages", null=True, blank=True
    )  # Nullable in case storage location is used outside of a run
    upload_storage_location = models.CharField(max_length=1024, blank=True, null=True)  # Original file path
    ocr_storage_location = models.CharField(max_length=1024, blank=True, null=True)  # OCR-processed file path

    def __str__(self):
        return f"Storage {self.storage_id} - {self.upload_storage_location}"


class OCRBatch(models.Model):
    """
    Represents a batch of pages that were processed in a single OCR job.
    This ensures better management of the smaller chunks or batches of pages.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ocr_file = models.ForeignKey(OCRFile, on_delete=models.CASCADE, related_name="ocr_batches")
    batch_filepath = models.CharField(max_length=1024, blank=False, null=False)
    batch_status = models.CharField(max_length=20, choices=[('Processing', 'Processing'), ('Completed', 'Completed'), ('Failed', 'Failed')], default='Processing')
    start_page = models.IntegerField()
    end_page = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"OCR Batch {self.id} - {self.batch_status}"

