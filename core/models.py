from django.db import models
from django.contrib.auth import get_user_model
import uuid
from django.contrib.postgres.fields import ArrayField
# from grid_documents_interrogation.models import Topic
import typing
if typing.TYPE_CHECKING:
    from grid_documents_interrogation.models import Topic

# from document_translation.models import TranslationRun
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey

from elasticsearch_dsl import Document, Date, Keyword, Text, Integer
from elasticsearch_dsl.connections import connections

User = get_user_model()


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="projects")
    project_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project_name} ({self.user.username})"


class Run(models.Model):
    """
    Represents a processing run for file uploads, translations, or other tasks.
    """
    run_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(
        max_length=50,
        choices=[
            ("Uploaded", "Uploaded"),
            ("Processing", "Processing"),
            ("Translating", "Translating"),
            ("Pending Approval", "Pending Approval"),
            ("Approved", "Approved"),
            ("Declined", "Declined"),
            ("Completed", "Completed"),
        ],
        default="Uploaded",
        db_index=True,
    )
    unique_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    characters = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Run {self.run_id} - {self.status}"



class Storage(models.Model):
    storage_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="storages", null=True)

    # 👇 Generic link to any run-like model: Run, TranslationRun, OCRRun, etc.
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True)
    object_id = models.UUIDField(null=True)  # Assumes your runs use UUIDField
    run_object = GenericForeignKey("content_type", "object_id")

    upload_storage_location = models.CharField(max_length=1024, blank=True, null=True)
    output_storage_location = models.CharField(max_length=1024, blank=True, null=True)

    def __str__(self):
        return f"Storage {self.storage_id} - {self.upload_storage_location}"


class File(models.Model):
    """
    Represents a document uploaded for processing.
    """
    id = models.AutoField(primary_key=True)
    filename = models.CharField(max_length=500)
    filepath = models.CharField(max_length=1024)  # ✅ Store absolute file path
    file_size = models.BigIntegerField(default=0)  # ✅ Store file size in bytes
    file_type = models.CharField(max_length=255, blank=True, null=True)  # ✅ Increased max_length to 255
    extension = models.CharField(max_length=10, null=True, blank=True)
    
    # md5_hash = models.CharField(max_length=32, unique=True, null=True, blank=True, help_text="MD5 checksum for duplicate detection")

    md5_hash = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        help_text="MD5 checksum for duplicate detection"
    )


    origin_file = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="copies",
        help_text="Reference to the original file if this is a user-initiated copy"
    )


    status = models.CharField(
        max_length=50,
        choices=[
            ("Pending", "Pending"),
            ("Processing", "Processing"),
            ("Completed", "Completed"),
            ("Failed", "Failed"),
        ],
        default="Pending",
        db_index=True,
    )
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="files", db_column="run_id")
    # topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="files", null=True, blank=True)
    # topic = models.ForeignKey("grid_documents_interrogation.Topic", on_delete=models.CASCADE)
    # topic = models.ForeignKey("grid_documents_interrogation.Topic", on_delete=models.SET_NULL, null=True, blank=True)
    content = models.TextField(null=True, blank=True, help_text="Extracted plain text content of the file.")
    topic = models.ForeignKey("grid_documents_interrogation.Topic", on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_files")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="files")
    project_id = models.CharField(max_length=255, db_index=True, blank=False)
    service_id = models.CharField(max_length=255, db_index=True, blank=False)
    storage = models.ForeignKey(Storage, on_delete=models.CASCADE, related_name="files", null=True, blank=True)  # ✅ Ensure Storage is referenced correctly
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    document_type = models.CharField(max_length=255, null=True, blank=True, help_text="Predicted document type, e.g. Contract, Financial Report, etc.")

    def __str__(self):
        return f"File: {self.filename} ({self.status})"

    def is_copy(self):
        return self.origin_file_id is not None

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "md5_hash"],
                name="uniq_file_md5_per_user"
            )
        ]

    @classmethod
    def make_copy(cls, original_file: "File", project_id: str, service_id: str) -> "File":
        """
        Create a logical copy of the given file for the same user.
        """
        new_run = Run.objects.create(user=original_file.user, status="Pending")

        return cls.objects.create(
            run=new_run,
            user=original_file.user,
            filename=f"{original_file.filename} (Copy)",
            filepath=original_file.filepath,
            file_size=original_file.file_size,
            file_type=original_file.file_type,
            md5_hash=original_file.md5_hash,  # allowed since constraint is (user, md5)
            storage=original_file.storage,
            project_id=project_id,
            service_id=service_id,
            origin_file=original_file,
        )


class Metadata(models.Model):
    """
    Stores metadata related to a file.
    """
    metadata_id = models.AutoField(primary_key=True)
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="metadata")
    storage = models.ForeignKey(Storage, on_delete=models.CASCADE, related_name="metadata", null=True, blank=True)

    # General File Info
    format = models.CharField(max_length=255, blank=True, null=True)
    title = models.CharField(max_length=255, blank=True, null=True)
    author = models.CharField(max_length=255, blank=True, null=True)
    subject = models.CharField(max_length=255, blank=True, null=True)
    keywords = models.TextField(blank=True, null=True)
    creator = models.CharField(max_length=255, blank=True, null=True)
    producer = models.CharField(max_length=255, blank=True, null=True)

    # Dates
    creationdate = models.DateTimeField(null=True, blank=True)
    moddate = models.DateTimeField(null=True, blank=True)

    # PDF Metadata
    trapped = models.CharField(max_length=255, blank=True, null=True)
    encryption = models.CharField(max_length=255, blank=True, null=True)
    file_size = models.CharField(max_length=255, blank=True, null=True)
    page_count = models.IntegerField(default=0, null=True)
    is_encrypted = models.BooleanField(default=False, null=True)
    pdf_version = models.CharField(max_length=20, blank=True, null=True)
    fonts = ArrayField(models.CharField(max_length=255), blank=True, null=True)

    # PDF Metadata Specific
    creator_pdf = models.CharField(max_length=255, blank=True, null=True)  # Creator (PDF Metadata)
    creationdate_pdf = models.DateTimeField(null=True, blank=True)  # Creationdate (PDF Metadata)
    moddate_pdf = models.DateTimeField(null=True, blank=True)  # Moddate (PDF Metadata)

    # Additional Metadata Fields
    page_rotation = models.JSONField(blank=True, null=True)
    custom_metadata = models.TextField(default=False, null=True)  # Custom Metadata
    metadata_stream = models.TextField(blank=True, null=True)
    tagged = models.TextField(default=False, null=True)
    userproperties = models.TextField(default=False, null=True)
    suspects = models.TextField(default=False, null=True)
    
    # New Fields
    form = models.CharField(max_length=255, blank=True, null=True)
    javascript = models.CharField(max_length=255, blank=True, null=True)
    pages = models.JSONField(blank=True, null=True)
    encrypted = models.BooleanField(default=False, null=True)  # Encrypted
    page_size = models.CharField(max_length=255, blank=True, null=True)
    optimized = models.BooleanField(default=False, null=True)

    # Optional Metadata
    category = models.CharField(max_length=255, blank=True, null=True)
    content_status = models.CharField(max_length=255, blank=True, null=True)
    revision = models.CharField(max_length=255, blank=True, null=True)
    last_modified_by = models.CharField(max_length=255, blank=True, null=True)

    # PDFMiner Metadata
    pdfminer_info = models.JSONField(blank=True, null=True)

    # Word Metadata
    word_count = models.IntegerField(default=0, null=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    
    # md5_hash = models.CharField(max_length=32, unique=True, null=True, blank=True, help_text="MD5 checksum for duplicate detection")
    md5_hash = models.CharField(max_length=32, null=True, blank=True, help_text="MD5 checksum for duplicate detection")
 
    

    def __str__(self):
        return self.title or f"Metadata {self.metadata_id}"

class EndpointResponseTable(models.Model):
    """
    Stores responses from various API endpoints for later retrieval.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="endpoint_responses")
    client = models.ForeignKey(User, on_delete=models.CASCADE, related_name="endpoint_responses")
    endpoint_name = models.CharField(max_length=255, db_index=True)  # e.g., "FileUploadView"
    response_data = models.JSONField(null=True, blank=True)  # ✅ Store response as JSON
    status = models.CharField(
        max_length=50,
        choices=[
            ("Pending", "Pending"),
            ("Processing", "Processing"),
            ("Completed", "Completed"),
            ("Failed", "Failed"),
        ],
        default="Pending",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.endpoint_name} - {self.run.run_id} ({self.status})"


class Webhook(models.Model):
    """
    Represents webhook configurations for users.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    webhook_url = models.URLField(max_length=255)
    secret_key = models.CharField(max_length=255, help_text="Used to sign messages for secure transmission")

    def __str__(self):
        return f"{self.user.username}'s webhook"


class BulkJob(models.Model):
    """
    Tracks a bulk/batch processing job across multiple files.
    """
    JOB_TYPES = [
        # Private Equity
        ("pe_classify", "Classify Documents"),
        ("pe_extract_risk_clauses", "Extract Risk Clauses"),
        ("pe_issue_spotting", "Issue Spotting"),
        ("pe_findings_report", "Findings Report"),
        # Class Actions
        ("ca_evidence_culling", "Evidence Culling"),
        ("ca_pii_redaction", "PII Redaction"),
        ("ca_extract_damages", "Extract Damages"),
        ("ca_issue_tagging", "Issue Tagging"),
        ("ca_duplicate_detection", "Duplicate Detection"),
        # Labor & Employment
        ("le_analyze_communications", "Analyze Communications"),
        ("le_wage_hour_analysis", "Wage & Hour Analysis"),
        ("le_policy_comparison", "Policy Comparison"),
        # IP Litigation
        ("ip_analyze_patent", "Patent Analysis"),
        ("ip_prior_art_search", "Prior Art Search"),
        ("ip_infringement", "Infringement Analysis"),
        # Regulatory Compliance
        ("rc_dsar_processing", "DSAR Processing"),
        ("rc_redaction", "Redaction"),
        ("rc_gap_analysis", "Gap Analysis"),
        ("rc_policy_mapping", "Policy Mapping"),
        # AI Document Processing
        ("ai_document_search", "Semantic Document Search"),
        ("ai_document_classification", "Document Classification"),
        ("ai_anonymization", "Document Anonymization"),
        ("ai_ocr", "OCR Text Extraction"),
        ("ai_translation", "Document Translation"),
        ("ai_document_structure", "Document Structure Analysis"),
        ("ai_redlining", "Document Redlining"),
        ("ai_metadata_extraction", "Metadata Extraction"),
    ]

    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("Queued", "Queued"),
        ("Processing", "Processing"),
        ("Completed", "Completed"),
        ("Failed", "Failed"),
        ("Partially_Completed", "Partially Completed"),
        ("Cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bulk_jobs")
    job_type = models.CharField(max_length=50, choices=JOB_TYPES, db_index=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="Pending", db_index=True)
    total_files = models.IntegerField(default=0)
    processed_files = models.IntegerField(default=0)
    failed_files = models.IntegerField(default=0)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    folder_id = models.UUIDField(null=True, blank=True, help_text="Process all files in this folder")
    input_parameters = models.JSONField(default=dict, blank=True, help_text="Service-specific parameters")
    result_summary = models.JSONField(default=dict, blank=True, help_text="Summary of processing results")
    error_log = models.JSONField(default=list, blank=True, help_text="List of errors with file details")
    celery_group_id = models.CharField(max_length=255, null=True, blank=True, help_text="Celery group ID for tracking")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"BulkJob({self.job_type}) {self.id} - {self.status} ({self.processed_files}/{self.total_files})"


class BulkJobFileResult(models.Model):
    """
    Tracks the result of processing a single file within a BulkJob.
    """
    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("Processing", "Processing"),
        ("Completed", "Completed"),
        ("Failed", "Failed"),
        ("Skipped", "Skipped"),
    ]

    id = models.BigAutoField(primary_key=True)
    bulk_job = models.ForeignKey(BulkJob, on_delete=models.CASCADE, related_name="file_results")
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="bulk_results")
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="Pending", db_index=True)
    result_data = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(null=True, blank=True)
    task_id = models.CharField(max_length=255, null=True, blank=True, help_text="Celery task ID")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["bulk_job", "status"]),
            models.Index(fields=["bulk_job", "file"]),
        ]

    def __str__(self):
        return f"BulkJobFileResult({self.bulk_job_id}, file={self.file_id}) - {self.status}"


class UserPreference(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="ui_preferences")
    sidebar_pinned = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"UI prefs for {self.user.username}"

