from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import json

User = get_user_model()


class MassClaimsRun(models.Model):
    """
    Represents a mass claims or class action case management run.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_mass_claims_runs')
    run = models.OneToOneField('core.Run', on_delete=models.CASCADE, related_name='mass_claims')
    case_name = models.CharField(max_length=255, help_text="Name of the class action case")
    case_number = models.CharField(max_length=100, help_text="Court case number")
    court_jurisdiction = models.CharField(max_length=255, blank=True, help_text="Court jurisdiction")
    
    case_type = models.CharField(
        max_length=100,
        choices=[
            ('consumer_protection', 'Consumer Protection'),
            ('securities', 'Securities'),
            ('antitrust', 'Antitrust'),
            ('employment', 'Employment'),
            ('data_breach', 'Data Breach'),
            ('product_liability', 'Product Liability'),
            ('environmental', 'Environmental'),
            ('other', 'Other')
        ],
        default='consumer_protection'
    )
    
    claim_deadline = models.DateTimeField(null=True, blank=True, help_text="Deadline for claim submissions")
    settlement_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    
    # Case status
    status = models.CharField(
        max_length=50,
        choices=[
            ('intake', 'Intake Phase'),
            ('discovery', 'Discovery Phase'),
            ('settlement', 'Settlement Phase'),
            ('distribution', 'Distribution Phase'),
            ('closed', 'Closed')
        ],
        default='intake'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_mass_claims_run'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'case_name']),
        ]

    def __str__(self):
        return f"{self.case_name} - {self.case_number}"


class IntakeForm(models.Model):
    """
    Stores intake form submissions from claimants.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_intake_forms')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_intake_forms')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='intake_forms', null=True, blank=True)
    
    # Claimant identification
    claimant_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    
    # Form data (flexible JSON structure)
    claimant_data = models.JSONField(default=dict, help_text="Flexible claimant form data")
    
    # Deduplication
    is_duplicate = models.BooleanField(default=False)
    duplicate_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='duplicates')
    duplicate_score = models.FloatField(null=True, blank=True, help_text="Similarity score for duplicate detection")
    
    # Processing status
    processing_status = models.CharField(
        max_length=50,
        choices=[
            ('pending', 'Pending Review'),
            ('processing', 'Processing'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('duplicate', 'Marked as Duplicate')
        ],
        default='pending'
    )
    
    # Validation
    is_valid = models.BooleanField(default=True)
    validation_errors = models.JSONField(default=list, blank=True)
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ca_processed_forms')

    class Meta:
        db_table = 'class_actions_intake_form'
        ordering = ['-submitted_at']
        indexes = [
            models.Index(fields=['client', '-submitted_at']),
            models.Index(fields=['client', 'processing_status']),
        ]

    def __str__(self):
        return f"Intake Form {self.claimant_id} - {self.mass_claims_run.case_name}"


class EvidenceDocument(models.Model):
    """
    Represents evidence documents with culling and relevance scoring.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_evidence_documents')
    file = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='ca_evidence', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_evidence_documents')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='evidence_documents', null=True, blank=True)
    
    evidence_type = models.CharField(
        max_length=100,
        choices=[
            ('email', 'Email'),
            ('chat_log', 'Chat Log'),
            ('document', 'Document'),
            ('financial_record', 'Financial Record'),
            ('communication', 'Communication'),
            ('contract', 'Contract'),
            ('policy', 'Policy'),
            ('other', 'Other')
        ]
    )
    
    # Culling and relevance
    is_culled = models.BooleanField(default=False, help_text="Whether document was culled (excluded)")
    cull_reason = models.TextField(blank=True, help_text="Reason for culling")
    relevance_score = models.FloatField(null=True, blank=True, help_text="AI-generated relevance score (0.0-1.0)")
    
    # Content analysis
    contains_pii = models.BooleanField(default=False)
    privilege_status = models.CharField(
        max_length=50,
        choices=[
            ('none', 'No Privilege'),
            ('attorney_client', 'Attorney-Client'),
            ('work_product', 'Work Product'),
            ('other', 'Other Privilege')
        ],
        default='none'
    )
    
    # Processing metadata
    processing_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_evidence_document'
        ordering = ['-relevance_score', '-created_at']
        indexes = [
            models.Index(fields=['client', '-relevance_score']),
            models.Index(fields=['client', 'evidence_type']),
        ]

    def __str__(self):
        return f"Evidence: {self.file.filename} - {self.get_evidence_type_display()}"


class PIIRedaction(models.Model):
    """
    Stores PII redaction information for documents.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_pii_redactions')
    file = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='ca_pii_redactions', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_pii_redactions')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='pii_redactions', null=True, blank=True)
    
    pii_type = models.CharField(
        max_length=50,
        choices=[
            ('name', 'Personal Name'),
            ('email', 'Email Address'),
            ('phone', 'Phone Number'),
            ('ssn', 'Social Security Number'),
            ('address', 'Physical Address'),
            ('credit_card', 'Credit Card Number'),
            ('bank_account', 'Bank Account Number'),
            ('driver_license', 'Driver License'),
            ('passport', 'Passport Number'),
            ('other', 'Other PII')
        ]
    )
    
    original_text = models.TextField(help_text="Original text containing PII")
    redacted_text = models.TextField(help_text="Text with PII redacted")
    
    # Location information
    page_number = models.IntegerField(help_text="Page number where PII was found")
    position_start = models.IntegerField(null=True, blank=True)
    position_end = models.IntegerField(null=True, blank=True)
    
    # Confidence and verification
    confidence_score = models.FloatField(help_text="AI confidence in PII detection (0.0-1.0)")
    is_verified = models.BooleanField(default=False, help_text="Human verification of redaction")
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ca_verified_redactions')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_pii_redaction'
        ordering = ['page_number', 'position_start']
        indexes = [
            models.Index(fields=['client', 'pii_type']),
        ]

    def __str__(self):
        return f"PII Redaction: {self.get_pii_type_display()} in {self.file.filename}"


class ExhibitPackage(models.Model):
    """
    Represents a package of documents for exhibit production with Bates stamping.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_exhibit_packages')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_exhibit_packages')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='exhibit_packages', null=True, blank=True)
    
    package_name = models.CharField(max_length=255, help_text="Name of the exhibit package")
    description = models.TextField(blank=True, help_text="Description of the package contents")
    
    # Files in the package
    files = models.ManyToManyField('core.File', related_name='ca_exhibit_packages')
    
    # Bates numbering
    bates_prefix = models.CharField(max_length=20, default="EXHIBIT", help_text="Bates number prefix")
    bates_start = models.CharField(max_length=50, help_text="Starting Bates number")
    bates_end = models.CharField(max_length=50, help_text="Ending Bates number")
    total_pages = models.IntegerField(default=0, help_text="Total pages in package")
    
    # Package status
    status = models.CharField(
        max_length=50,
        choices=[
            ('draft', 'Draft'),
            ('processing', 'Processing'),
            ('ready', 'Ready for Production'),
            ('produced', 'Produced'),
            ('archived', 'Archived')
        ],
        default='draft'
    )
    
    # Production metadata
    production_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    produced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'class_actions_exhibit_package'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"{self.package_name} ({self.bates_start} - {self.bates_end})"


class SettlementTracking(models.Model):
    """
    Tracks settlement and notice information for mass claims.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_settlement_tracking')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='settlement_tracking', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_settlement_tracking')
    
    # Settlement details
    settlement_type = models.CharField(
        max_length=50,
        choices=[
            ('monetary', 'Monetary Settlement'),
            ('injunctive', 'Injunctive Relief'),
            ('mixed', 'Mixed Settlement'),
            ('other', 'Other')
        ],
        default='monetary'
    )
    
    total_settlement_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    attorney_fees = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    administration_costs = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    net_settlement_fund = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    
    # Notice information
    notice_type = models.CharField(
        max_length=50,
        choices=[
            ('publication', 'Publication Notice'),
            ('direct_mail', 'Direct Mail'),
            ('email', 'Email Notice'),
            ('website', 'Website Notice'),
            ('combined', 'Combined Methods')
        ],
        default='publication'
    )
    
    notice_deadline = models.DateTimeField(null=True, blank=True)
    objection_deadline = models.DateTimeField(null=True, blank=True)
    opt_out_deadline = models.DateTimeField(null=True, blank=True)
    
    # Distribution tracking
    total_eligible_claimants = models.IntegerField(default=0)
    total_approved_claims = models.IntegerField(default=0)
    total_distributed_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ('pending_approval', 'Pending Court Approval'),
            ('approved', 'Court Approved'),
            ('notice_period', 'Notice Period'),
            ('distribution', 'Distribution Phase'),
            ('completed', 'Completed')
        ],
        default='pending_approval'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_settlement_tracking'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"Settlement Tracking - {self.mass_claims_run.case_name}"


class ClaimantCommunication(models.Model):
    """
    Tracks communications with claimants throughout the process.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_claimant_communications')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_claimant_communications')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='claimant_communications', null=True, blank=True)
    intake_form = models.ForeignKey(IntakeForm, on_delete=models.CASCADE, related_name='communications', null=True, blank=True)
    
    communication_type = models.CharField(
        max_length=50,
        choices=[
            ('email', 'Email'),
            ('mail', 'Physical Mail'),
            ('phone', 'Phone Call'),
            ('sms', 'SMS'),
            ('portal', 'Web Portal Message')
        ]
    )
    
    subject = models.CharField(max_length=255, blank=True)
    message_content = models.TextField()
    
    # Status tracking
    sent_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    
    # Response tracking
    response_content = models.TextField(blank=True)
    requires_follow_up = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_claimant_communication'
        ordering = ['-sent_at']
        indexes = [
            models.Index(fields=['client', '-sent_at']),
        ]

    def __str__(self):
        return f"{self.get_communication_type_display()} - {self.intake_form.claimant_id}"


# ═══════════════════════════════════════════════════════════════
# 🔄 COMPREHENSIVE SERVICE OUTPUT MODELS
# ═══════════════════════════════════════════════════════════════

class ServiceExecution(models.Model):
    """
    Tracks execution of all Class Actions services with comprehensive metadata.
    Multi-tenant: Isolated by client.
    """
    SERVICE_TYPES = [
        # Core Class Actions Services
        ('ca-create-claims-run', 'Create Mass Claims Run'),
        ('ca-evidence-culling', 'Evidence Culling & Relevance'),
        ('ca-pii-redaction', 'PII Redaction & Privacy'),
        ('ca-duplicate-detection', 'Duplicate Detection'),
        ('ca-claims-dashboard', 'Claims Analysis Dashboard'),

        # AI-Enhanced Class Actions Services
        ('ca-mass-upload', 'Mass Document Upload'),
        ('ca-evidence-search', 'Evidence Document Search'),
        ('ca-document-qa', 'Evidence Q&A System'),
        ('ca-structure-analysis', 'Evidence Structure Analysis'),
        ('ca-file-management', 'Case File Management'),
        ('ca-client-summary', 'Case Statistics Summary'),
        ('ca-ocr-evidence', 'Evidence Document OCR'),
    ]

    EXECUTION_STATUS = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    OUTPUT_TYPES = [
        ('document', 'Document'),
        ('pdf', 'PDF'),
        ('text', 'Text'),
        ('json', 'JSON Data'),
        ('data', 'Data File'),
        ('analytics', 'Analytics'),
        ('report', 'Report'),
        ('chart', 'Chart'),
        ('dashboard', 'Dashboard'),
        ('excel', 'Excel'),
        ('matrix', 'Matrix'),
        ('package', 'Package'),
    ]

    # Core identification
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_service_executions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ca_service_executions')
    mass_claims_run = models.ForeignKey(MassClaimsRun, on_delete=models.CASCADE, related_name='service_executions', null=True, blank=True)

    # Service details
    service_type = models.CharField(max_length=50, choices=SERVICE_TYPES)
    service_name = models.CharField(max_length=255, help_text="Human-readable service name")
    service_version = models.CharField(max_length=20, default='1.0')

    # Execution tracking
    status = models.CharField(max_length=20, choices=EXECUTION_STATUS, default='pending')
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    execution_time_seconds = models.IntegerField(null=True, blank=True)

    # Input/Output tracking
    input_files = models.ManyToManyField('core.File', blank=True, related_name='ca_service_inputs')
    input_parameters = models.JSONField(default=dict, help_text="Service input parameters")
    output_type = models.CharField(max_length=20, choices=OUTPUT_TYPES, default='json')
    output_count = models.IntegerField(default=0, help_text="Number of output files generated")

    # Error handling
    error_message = models.TextField(blank=True)
    error_traceback = models.TextField(blank=True)

    # Metadata
    execution_metadata = models.JSONField(default=dict, help_text="Additional execution metadata")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_actions_service_execution'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['client', '-started_at']),
            models.Index(fields=['client', 'service_type']),
            models.Index(fields=['user', 'service_type']),
            models.Index(fields=['mass_claims_run', 'status']),
            models.Index(fields=['started_at']),
        ]

    def __str__(self):
        return f"{self.service_name} - {self.get_status_display()}"

    @property
    def duration(self):
        """Calculate execution duration"""
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


class ServiceOutput(models.Model):
    """
    Stores individual output files/data from service executions.
    Multi-tenant: Isolated by client.
    """
    # Core identification
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ca_service_outputs')
    service_execution = models.ForeignKey(ServiceExecution, on_delete=models.CASCADE, related_name='outputs', null=True, blank=True)

    # Output details
    output_name = models.CharField(max_length=255, help_text="Name/title of the output")
    output_type = models.CharField(max_length=20, choices=ServiceExecution.OUTPUT_TYPES)
    file_extension = models.CharField(max_length=10, blank=True, help_text="File extension if applicable")
    mime_type = models.CharField(max_length=100, blank=True)

    # File storage
    output_file = models.FileField(upload_to='ca_service_outputs/%Y/%m/%d/', null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True, help_text="File size in bytes")

    # Data storage (for non-file outputs)
    output_data = models.JSONField(null=True, blank=True, help_text="Structured data output")
    output_text = models.TextField(blank=True, help_text="Text-based output")

    # URLs and access
    download_url = models.URLField(blank=True, help_text="Direct download URL")
    preview_url = models.URLField(blank=True, help_text="Preview URL")

    # Metadata
    output_metadata = models.JSONField(default=dict, help_text="Additional output metadata")
    is_primary = models.BooleanField(default=False, help_text="Primary output for the service")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'class_actions_service_output'
        ordering = ['-is_primary', '-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['service_execution', 'output_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.output_name} ({self.get_output_type_display()})"

    @property
    def formatted_size(self):
        """Return human-readable file size"""
        if not self.file_size:
            return "Unknown"

        size = self.file_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
