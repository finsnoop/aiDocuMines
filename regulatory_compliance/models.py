from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import json

User = get_user_model()


class ComplianceRun(models.Model):
    """
    Represents a regulatory compliance analysis run.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_compliance_runs')
    run = models.OneToOneField('core.Run', on_delete=models.CASCADE, related_name='compliance_analysis')
    compliance_framework = models.CharField(
        max_length=100,
        choices=[
            ('gdpr', 'GDPR (General Data Protection Regulation)'),
            ('ccpa', 'CCPA (California Consumer Privacy Act)'),
            ('hipaa', 'HIPAA (Health Insurance Portability and Accountability Act)'),
            ('sox', 'SOX (Sarbanes-Oxley Act)'),
            ('pci_dss', 'PCI DSS (Payment Card Industry Data Security Standard)'),
            ('iso27001', 'ISO 27001 (Information Security Management)'),
            ('nist', 'NIST Cybersecurity Framework'),
            ('custom', 'Custom Compliance Framework')
        ]
    )
    
    organization_name = models.CharField(max_length=255, help_text="Name of the organization being assessed")
    assessment_scope = models.TextField(help_text="Scope of the compliance assessment")
    
    # Regulatory requirements
    applicable_regulations = models.JSONField(default=list, help_text="List of applicable regulations")
    jurisdiction = models.CharField(max_length=100, blank=True, help_text="Legal jurisdiction")
    
    # Assessment period
    assessment_start_date = models.DateField(null=True, blank=True, help_text="Start date of assessment period")
    assessment_end_date = models.DateField(null=True, blank=True, help_text="End date of assessment period")
    
    # Key stakeholders
    compliance_officer = models.CharField(max_length=255, blank=True, help_text="Primary compliance officer")
    legal_counsel = models.CharField(max_length=255, blank=True, help_text="Legal counsel contact")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_run'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'compliance_framework']),
        ]

    def __str__(self):
        return f"{self.organization_name} - {self.get_compliance_framework_display()}"


class RegulatoryRequirement(models.Model):
    """
    Individual regulatory requirements and controls.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_regulatory_requirements')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='regulatory_requirements')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='regulatory_requirements', null=True, blank=True)
    
    requirement_id = models.CharField(max_length=100, help_text="Unique identifier for the requirement")
    requirement_title = models.CharField(max_length=500, help_text="Title of the regulatory requirement")
    requirement_text = models.TextField(help_text="Full text of the requirement")
    
    # Requirement categorization
    category = models.CharField(
        max_length=100,
        choices=[
            ('data_protection', 'Data Protection'),
            ('privacy_rights', 'Privacy Rights'),
            ('security_controls', 'Security Controls'),
            ('breach_notification', 'Breach Notification'),
            ('consent_management', 'Consent Management'),
            ('data_retention', 'Data Retention'),
            ('cross_border_transfer', 'Cross-Border Data Transfer'),
            ('vendor_management', 'Vendor Management'),
            ('audit_logging', 'Audit and Logging'),
            ('training_awareness', 'Training and Awareness'),
            ('other', 'Other')
        ]
    )
    
    # Compliance status
    compliance_status = models.CharField(
        max_length=50,
        choices=[
            ('compliant', 'Compliant'),
            ('non_compliant', 'Non-Compliant'),
            ('partially_compliant', 'Partially Compliant'),
            ('not_applicable', 'Not Applicable'),
            ('under_review', 'Under Review')
        ],
        default='under_review'
    )
    
    # Risk assessment
    risk_level = models.CharField(
        max_length=20,
        choices=[
            ('critical', 'Critical'),
            ('high', 'High'),
            ('medium', 'Medium'),
            ('low', 'Low')
        ],
        default='medium'
    )
    
    # Implementation details
    implementation_notes = models.TextField(blank=True, help_text="Notes on current implementation")
    remediation_plan = models.TextField(blank=True, help_text="Plan for addressing non-compliance")
    responsible_party = models.CharField(max_length=255, blank=True, help_text="Person/team responsible")
    
    # Dates
    due_date = models.DateField(null=True, blank=True, help_text="Compliance due date")
    last_reviewed = models.DateField(null=True, blank=True, help_text="Last review date")
    next_review = models.DateField(null=True, blank=True, help_text="Next scheduled review")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_requirement'
        ordering = ['requirement_id']
        unique_together = ['compliance_run', 'requirement_id']
        indexes = [
            models.Index(fields=['client', 'requirement_id']),
            models.Index(fields=['client', 'category']),
            models.Index(fields=['client', 'compliance_status']),
        ]

    def __str__(self):
        return f"{self.requirement_id} - {self.requirement_title[:50]}..."


class PolicyMapping(models.Model):
    """
    Mapping between organizational policies and regulatory requirements.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_policy_mappings')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='policy_mappings')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='policy_mappings', null=True, blank=True)
    regulatory_requirement = models.ForeignKey(RegulatoryRequirement, on_delete=models.CASCADE, related_name='policy_mappings', null=True, blank=True)
    
    policy_document = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='policy_mappings', null=True, blank=True)
    policy_name = models.CharField(max_length=255, help_text="Name of the policy document")
    policy_section = models.CharField(max_length=255, blank=True, help_text="Specific section of the policy")
    
    # Mapping analysis
    mapping_strength = models.CharField(
        max_length=20,
        choices=[
            ('strong', 'Strong Mapping'),
            ('moderate', 'Moderate Mapping'),
            ('weak', 'Weak Mapping'),
            ('none', 'No Mapping')
        ]
    )
    
    gap_analysis = models.TextField(blank=True, help_text="Analysis of gaps between policy and requirement")
    recommendations = models.JSONField(default=list, help_text="Recommendations for improving compliance")
    
    # Mapping metadata
    mapping_confidence = models.FloatField(null=True, blank=True, help_text="Confidence score (0.0-1.0)")
    mapping_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_policy_mapping'
        ordering = ['-mapping_confidence']
        indexes = [
            models.Index(fields=['client', '-mapping_confidence']),
            models.Index(fields=['client', 'mapping_strength']),
        ]

    def __str__(self):
        return f"{self.policy_name} -> {self.regulatory_requirement.requirement_id}"


class DSARRequest(models.Model):
    """
    Data Subject Access Request (DSAR) for GDPR and other privacy regulations.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_dsar_requests')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dsar_requests')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='dsar_requests', null=True, blank=True)
    
    # Request identification
    request_id = models.CharField(max_length=100, unique=True, help_text="Unique DSAR request identifier")
    request_type = models.CharField(
        max_length=50,
        choices=[
            ('access', 'Right of Access'),
            ('rectification', 'Right of Rectification'),
            ('erasure', 'Right of Erasure (Right to be Forgotten)'),
            ('portability', 'Right to Data Portability'),
            ('restriction', 'Right to Restriction of Processing'),
            ('objection', 'Right to Object'),
            ('automated_decision', 'Rights Related to Automated Decision Making')
        ]
    )
    
    # Data subject information
    data_subject_name = models.CharField(max_length=255, help_text="Name of the data subject")
    data_subject_email = models.EmailField(help_text="Email of the data subject")
    data_subject_id = models.CharField(max_length=255, blank=True, help_text="Internal ID for data subject")
    
    # Request details
    request_date = models.DateTimeField(help_text="Date when request was received")
    request_description = models.TextField(help_text="Description of the request")
    verification_status = models.CharField(
        max_length=50,
        choices=[
            ('pending', 'Pending Verification'),
            ('verified', 'Verified'),
            ('rejected', 'Verification Rejected')
        ],
        default='pending'
    )
    
    # Processing status
    status = models.CharField(
        max_length=50,
        choices=[
            ('received', 'Received'),
            ('in_progress', 'In Progress'),
            ('data_collection', 'Data Collection'),
            ('review', 'Under Review'),
            ('completed', 'Completed'),
            ('rejected', 'Rejected'),
            ('extended', 'Extended (Additional Time Required)')
        ],
        default='received'
    )
    
    # Response details
    response_due_date = models.DateTimeField(help_text="Due date for response (typically 30 days)")
    response_date = models.DateTimeField(null=True, blank=True, help_text="Date response was provided")
    response_method = models.CharField(
        max_length=50,
        choices=[
            ('email', 'Email'),
            ('postal_mail', 'Postal Mail'),
            ('secure_portal', 'Secure Portal'),
            ('in_person', 'In Person')
        ],
        blank=True
    )
    
    # Data collection results
    data_sources_searched = models.JSONField(default=list, help_text="List of data sources searched")
    personal_data_found = models.BooleanField(default=False, help_text="Whether personal data was found")
    data_categories = models.JSONField(default=list, help_text="Categories of personal data found")
    
    # Processing notes
    processing_notes = models.TextField(blank=True, help_text="Notes on request processing")
    rejection_reason = models.TextField(blank=True, help_text="Reason for rejection if applicable")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_dsar_request'
        ordering = ['-request_date']
        indexes = [
            models.Index(fields=['client', '-request_date']),
            models.Index(fields=['client', 'request_type']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"DSAR-{self.request_id} - {self.data_subject_name} ({self.get_request_type_display()})"


class DataInventory(models.Model):
    """
    Data inventory for tracking personal data processing activities.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_data_inventories')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='data_inventories')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='data_inventories', null=True, blank=True)
    
    # Data processing activity
    activity_name = models.CharField(max_length=255, help_text="Name of the data processing activity")
    activity_description = models.TextField(help_text="Description of the processing activity")
    
    # Data categories
    data_categories = models.JSONField(default=list, help_text="Categories of personal data processed")
    special_categories = models.JSONField(default=list, help_text="Special categories of personal data")
    
    # Data subjects
    data_subject_categories = models.JSONField(default=list, help_text="Categories of data subjects")
    
    # Legal basis
    legal_basis = models.CharField(
        max_length=100,
        choices=[
            ('consent', 'Consent'),
            ('contract', 'Performance of Contract'),
            ('legal_obligation', 'Legal Obligation'),
            ('vital_interests', 'Vital Interests'),
            ('public_task', 'Public Task'),
            ('legitimate_interests', 'Legitimate Interests')
        ]
    )
    
    # Processing details
    processing_purposes = models.JSONField(default=list, help_text="Purposes of processing")
    data_sources = models.JSONField(default=list, help_text="Sources of personal data")
    data_recipients = models.JSONField(default=list, help_text="Recipients of personal data")
    
    # International transfers
    international_transfers = models.BooleanField(default=False, help_text="Whether data is transferred internationally")
    transfer_countries = models.JSONField(default=list, help_text="Countries to which data is transferred")
    transfer_safeguards = models.JSONField(default=list, help_text="Safeguards for international transfers")
    
    # Retention
    retention_period = models.CharField(max_length=255, blank=True, help_text="Data retention period")
    retention_criteria = models.TextField(blank=True, help_text="Criteria for determining retention period")
    
    # Security measures
    technical_measures = models.JSONField(default=list, help_text="Technical security measures")
    organizational_measures = models.JSONField(default=list, help_text="Organizational security measures")
    
    # Data protection impact assessment
    dpia_required = models.BooleanField(default=False, help_text="Whether DPIA is required")
    dpia_completed = models.BooleanField(default=False, help_text="Whether DPIA has been completed")
    dpia_document = models.ForeignKey('core.File', on_delete=models.SET_NULL, null=True, blank=True, related_name='dpia_inventories')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_data_inventory'
        ordering = ['activity_name']
        indexes = [
            models.Index(fields=['client', 'activity_name']),
            models.Index(fields=['client', 'legal_basis']),
        ]

    def __str__(self):
        return f"{self.activity_name} - {self.get_legal_basis_display()}"


class RedactionTask(models.Model):
    """
    Document redaction tasks for privacy protection.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_redaction_tasks')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='redaction_tasks')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='redaction_tasks', null=True, blank=True)
    
    task_name = models.CharField(max_length=255, help_text="Name of the redaction task")
    source_document = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='redaction_tasks', null=True, blank=True)
    
    # Redaction parameters
    redaction_type = models.CharField(
        max_length=50,
        choices=[
            ('pii', 'Personally Identifiable Information'),
            ('phi', 'Protected Health Information'),
            ('financial', 'Financial Information'),
            ('legal_privilege', 'Legally Privileged Information'),
            ('trade_secret', 'Trade Secrets'),
            ('custom', 'Custom Redaction Rules')
        ]
    )
    
    # Redaction rules
    redaction_rules = models.JSONField(default=list, help_text="Rules for what to redact")
    redaction_patterns = models.JSONField(default=list, help_text="Regex patterns for redaction")
    
    # Processing status
    status = models.CharField(
        max_length=50,
        choices=[
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('review_required', 'Review Required')
        ],
        default='pending'
    )
    
    # Results
    redacted_document = models.ForeignKey('core.File', on_delete=models.SET_NULL, null=True, blank=True, related_name='redacted_from_tasks')
    redaction_count = models.IntegerField(default=0, help_text="Number of redactions made")
    redaction_summary = models.JSONField(default=dict, help_text="Summary of redactions by type")
    
    # Quality assurance
    qa_required = models.BooleanField(default=True, help_text="Whether QA review is required")
    qa_completed = models.BooleanField(default=False, help_text="Whether QA has been completed")
    qa_reviewer = models.CharField(max_length=255, blank=True, help_text="QA reviewer name")
    qa_notes = models.TextField(blank=True, help_text="QA review notes")
    
    # Processing metadata
    processing_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_redaction_task'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"{self.task_name} - {self.get_redaction_type_display()}"


class ComplianceAlert(models.Model):
    """
    Compliance alerts for regulatory violations or risks.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_compliance_alerts')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='compliance_alerts')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='compliance_alerts', null=True, blank=True)
    
    alert_type = models.CharField(
        max_length=100,
        choices=[
            ('deadline_approaching', 'Compliance Deadline Approaching'),
            ('violation_detected', 'Potential Violation Detected'),
            ('policy_gap', 'Policy Gap Identified'),
            ('dsar_overdue', 'DSAR Response Overdue'),
            ('data_breach', 'Data Breach Detected'),
            ('consent_expired', 'Consent Expiration'),
            ('retention_exceeded', 'Data Retention Period Exceeded'),
            ('unauthorized_transfer', 'Unauthorized Data Transfer'),
            ('missing_dpia', 'Missing Data Protection Impact Assessment'),
            ('training_required', 'Compliance Training Required')
        ]
    )
    
    alert_title = models.CharField(max_length=255, help_text="Title of the alert")
    alert_description = models.TextField(help_text="Detailed description of the alert")
    
    # Severity and priority
    severity = models.CharField(
        max_length=20,
        choices=[
            ('critical', 'Critical'),
            ('high', 'High'),
            ('medium', 'Medium'),
            ('low', 'Low')
        ]
    )
    
    priority = models.CharField(
        max_length=20,
        choices=[
            ('urgent', 'Urgent'),
            ('high', 'High'),
            ('medium', 'Medium'),
            ('low', 'Low')
        ]
    )
    
    # Related objects
    related_requirement = models.ForeignKey(RegulatoryRequirement, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    related_dsar = models.ForeignKey(DSARRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts')
    related_documents = models.ManyToManyField('core.File', related_name='compliance_alerts', blank=True)
    
    # Alert status
    status = models.CharField(
        max_length=50,
        choices=[
            ('open', 'Open'),
            ('in_progress', 'In Progress'),
            ('resolved', 'Resolved'),
            ('dismissed', 'Dismissed'),
            ('escalated', 'Escalated')
        ],
        default='open'
    )
    
    # Resolution details
    assigned_to = models.CharField(max_length=255, blank=True, help_text="Person assigned to resolve alert")
    resolution_notes = models.TextField(blank=True, help_text="Notes on alert resolution")
    resolved_at = models.DateTimeField(null=True, blank=True, help_text="Date alert was resolved")
    
    # Due date
    due_date = models.DateTimeField(null=True, blank=True, help_text="Due date for resolution")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'regulatory_compliance_alert'
        ordering = ['-severity', '-created_at']
        indexes = [
            models.Index(fields=['client', '-severity', '-created_at']),
            models.Index(fields=['client', 'alert_type']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"{self.alert_title} - {self.get_severity_display()}"


# ═══════════════════════════════════════════════════════════════
# 🔄 COMPREHENSIVE SERVICE OUTPUT MODELS
# ═══════════════════════════════════════════════════════════════

class ServiceExecution(models.Model):
    """
    Tracks execution of all Regulatory Compliance services with comprehensive metadata.
    """
    SERVICE_TYPES = [
        # Core Regulatory Compliance Services
        ('rc-create-compliance-run', 'Create Compliance Run'),
        ('rc-gap-analysis', 'Compliance Gap Analysis'),
        ('rc-policy-mapping', 'Policy Mapping & Alignment'),
        ('rc-audit-trail', 'Audit Trail Generation'),
        ('rc-compliance-dashboard', 'Compliance Dashboard'),

        # AI-Enhanced Regulatory Compliance Services
        ('rc-regulatory-search', 'Regulatory Document Search'),
        ('rc-document-qa', 'Compliance Q&A System'),
        ('rc-structure-analysis', 'Regulatory Structure Analysis'),
        ('rc-file-insights', 'Compliance File Insights'),
        ('rc-document-anonymization', 'Compliance Anonymization'),
        ('rc-ocr-regulatory', 'Regulatory Filing OCR'),
        ('rc-project-summary', 'Compliance Project Summary'),
        ('rc-service-summary', 'Compliance Audit Reports'),
        ('rc-client-summary', 'Compliance Client Summary'),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_service_executions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='rc_service_executions')
    compliance_run = models.ForeignKey(ComplianceRun, on_delete=models.CASCADE, related_name='service_executions', null=True, blank=True)

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
    input_files = models.ManyToManyField('core.File', blank=True, related_name='rc_service_inputs')
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
        db_table = 'regulatory_compliance_service_execution'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'service_type']),
            models.Index(fields=['compliance_run', 'status']),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='rc_service_outputs')
    service_execution = models.ForeignKey(ServiceExecution, on_delete=models.CASCADE, related_name='outputs', null=True, blank=True)

    # Output details
    output_name = models.CharField(max_length=255, help_text="Name/title of the output")
    output_type = models.CharField(max_length=20, choices=ServiceExecution.OUTPUT_TYPES)
    file_extension = models.CharField(max_length=10, blank=True, help_text="File extension if applicable")
    mime_type = models.CharField(max_length=100, blank=True)

    # File storage
    output_file = models.FileField(upload_to='rc_service_outputs/%Y/%m/%d/', null=True, blank=True)
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
        db_table = 'regulatory_compliance_service_output'
        ordering = ['-is_primary', '-created_at']
        indexes = [
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
