from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import json

User = get_user_model()


class WorkplaceCommunicationsRun(models.Model):
    """
    Represents a workplace communications analysis run for employment matters.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_communications_runs')
    run = models.OneToOneField('core.Run', on_delete=models.CASCADE, related_name='workplace_communications')
    case_name = models.CharField(max_length=255, help_text="Name of the employment case")
    company_name = models.CharField(max_length=255, help_text="Company being analyzed")
    
    case_type = models.CharField(
        max_length=100,
        choices=[
            ('discrimination', 'Discrimination'),
            ('harassment', 'Harassment'),
            ('wrongful_termination', 'Wrongful Termination'),
            ('wage_hour', 'Wage and Hour'),
            ('retaliation', 'Retaliation'),
            ('whistleblower', 'Whistleblower'),
            ('policy_violation', 'Policy Violation'),
            ('other', 'Other')
        ],
        default='discrimination'
    )
    
    # Data sources
    data_sources = models.JSONField(
        default=list,
        help_text="List of data sources (O365, Slack, Teams, etc.)"
    )
    
    # Analysis parameters
    analysis_start_date = models.DateTimeField(null=True, blank=True, help_text="Start date for communications analysis")
    analysis_end_date = models.DateTimeField(null=True, blank=True, help_text="End date for communications analysis")
    
    # Key personnel
    key_personnel = models.JSONField(
        default=list,
        help_text="List of key personnel involved in the case"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_communications_run'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'case_name']),
        ]

    def __str__(self):
        return f"{self.case_name} - {self.company_name}"


class CommunicationMessage(models.Model):
    """
    Represents individual communication messages (emails, chats, etc.).
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_communication_messages')
    file = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='le_communication_messages', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_communication_messages')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='messages', null=True, blank=True)
    
    # Message metadata
    message_id = models.CharField(max_length=255, help_text="Unique message identifier from source system")
    message_type = models.CharField(
        max_length=50,
        choices=[
            ('email', 'Email'),
            ('slack', 'Slack Message'),
            ('teams', 'Microsoft Teams'),
            ('chat', 'Chat Message'),
            ('sms', 'SMS'),
            ('other', 'Other')
        ]
    )
    
    # Participants
    sender = models.CharField(max_length=255, help_text="Message sender")
    recipients = models.JSONField(default=list, help_text="List of message recipients")
    
    # Content
    subject = models.CharField(max_length=500, blank=True, help_text="Message subject (for emails)")
    content = models.TextField(help_text="Message content/body")
    
    # Timestamps
    sent_datetime = models.DateTimeField(help_text="When the message was sent")
    
    # Analysis results
    sentiment_score = models.FloatField(null=True, blank=True, help_text="Sentiment analysis score (-1.0 to 1.0)")
    toxicity_score = models.FloatField(null=True, blank=True, help_text="Toxicity score (0.0 to 1.0)")
    relevance_score = models.FloatField(null=True, blank=True, help_text="Relevance to case (0.0 to 1.0)")
    
    # Flags
    is_privileged = models.BooleanField(default=False)
    contains_pii = models.BooleanField(default=False)
    is_flagged = models.BooleanField(default=False, help_text="Flagged for review")
    flag_reason = models.TextField(blank=True, help_text="Reason for flagging")
    
    # Processing metadata
    processing_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_communication_message'
        ordering = ['-sent_datetime']
        unique_together = ['message_id', 'communications_run']
        indexes = [
            models.Index(fields=['client', '-sent_datetime']),
            models.Index(fields=['client', 'message_type']),
        ]

    def __str__(self):
        return f"{self.message_type}: {self.sender} - {self.sent_datetime}"


class WageHourAnalysis(models.Model):
    """
    Stores wage and hour analysis results for employees.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_wage_hour_analyses')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_wage_hour_analyses')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='wage_hour_analyses', null=True, blank=True)
    
    # Employee information
    employee_name = models.CharField(max_length=255)
    employee_id = models.CharField(max_length=100, blank=True)
    job_title = models.CharField(max_length=255, blank=True)
    department = models.CharField(max_length=255, blank=True)
    
    # Analysis period
    analysis_start_date = models.DateField()
    analysis_end_date = models.DateField()
    
    # Work time analysis
    total_hours_worked = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    regular_hours = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    overtime_hours = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Communication-based time tracking
    early_morning_messages = models.IntegerField(default=0, help_text="Messages sent before 7 AM")
    late_evening_messages = models.IntegerField(default=0, help_text="Messages sent after 7 PM")
    weekend_messages = models.IntegerField(default=0, help_text="Messages sent on weekends")
    
    # Wage calculations
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    regular_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    overtime_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Potential violations
    potential_overtime_violations = models.BooleanField(default=False)
    potential_break_violations = models.BooleanField(default=False)
    potential_meal_violations = models.BooleanField(default=False)
    
    # Analysis metadata
    analysis_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_wage_hour_analysis'
        ordering = ['-created_at']
        unique_together = ['employee_name', 'communications_run', 'analysis_start_date']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'employee_name']),
        ]

    def __str__(self):
        return f"Wage Analysis: {self.employee_name} ({self.analysis_start_date} - {self.analysis_end_date})"


class PolicyComparison(models.Model):
    """
    Compares company policies against communications and best practices.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_policy_comparisons')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_policy_comparisons')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='policy_comparisons', null=True, blank=True)
    
    policy_name = models.CharField(max_length=255, help_text="Name of the policy being analyzed")
    policy_type = models.CharField(
        max_length=100,
        choices=[
            ('harassment', 'Harassment Policy'),
            ('discrimination', 'Discrimination Policy'),
            ('code_of_conduct', 'Code of Conduct'),
            ('social_media', 'Social Media Policy'),
            ('communication', 'Communication Policy'),
            ('remote_work', 'Remote Work Policy'),
            ('overtime', 'Overtime Policy'),
            ('other', 'Other Policy')
        ]
    )
    
    # Policy content
    policy_document = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='le_policy_comparisons', null=True, blank=True)
    policy_text = models.TextField(help_text="Extracted policy text")
    
    # Comparison results
    compliance_score = models.FloatField(help_text="Compliance score (0.0 to 1.0)")
    
    # Violations found
    violations_found = models.JSONField(default=list, help_text="List of policy violations found in communications")
    
    # Recommendations
    recommendations = models.JSONField(default=list, help_text="Policy improvement recommendations")
    
    # Best practices comparison
    best_practices_score = models.FloatField(null=True, blank=True, help_text="Score against industry best practices")
    missing_elements = models.JSONField(default=list, help_text="Missing policy elements")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_policy_comparison'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'policy_type']),
        ]

    def __str__(self):
        return f"Policy Analysis: {self.policy_name} - {self.get_policy_type_display()}"


class EEOCPacket(models.Model):
    """
    Generates EEOC complaint packets with relevant communications and analysis.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_eeoc_packets')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_eeoc_packets')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='eeoc_packets', null=True, blank=True)
    
    packet_name = models.CharField(max_length=255, help_text="Name of the EEOC packet")
    
    # Complainant information
    complainant_name = models.CharField(max_length=255)
    complainant_title = models.CharField(max_length=255, blank=True)
    complainant_department = models.CharField(max_length=255, blank=True)
    
    # Complaint details
    complaint_type = models.CharField(
        max_length=100,
        choices=[
            ('race', 'Race Discrimination'),
            ('gender', 'Gender Discrimination'),
            ('age', 'Age Discrimination'),
            ('disability', 'Disability Discrimination'),
            ('religion', 'Religious Discrimination'),
            ('national_origin', 'National Origin Discrimination'),
            ('sexual_harassment', 'Sexual Harassment'),
            ('retaliation', 'Retaliation'),
            ('other', 'Other')
        ]
    )
    
    incident_date = models.DateField(help_text="Date of the alleged incident")
    complaint_summary = models.TextField(help_text="Summary of the complaint")
    
    # Evidence collection
    relevant_messages = models.ManyToManyField(CommunicationMessage, related_name='eeoc_packets')
    supporting_documents = models.ManyToManyField('core.File', related_name='le_eeoc_packets')
    
    # Analysis results
    evidence_strength_score = models.FloatField(null=True, blank=True, help_text="Strength of evidence (0.0 to 1.0)")
    timeline_analysis = models.JSONField(default=dict, help_text="Timeline of relevant events")
    key_findings = models.JSONField(default=list, help_text="Key findings from analysis")
    
    # Packet status
    status = models.CharField(
        max_length=50,
        choices=[
            ('draft', 'Draft'),
            ('review', 'Under Review'),
            ('ready', 'Ready for Submission'),
            ('submitted', 'Submitted'),
            ('closed', 'Closed')
        ],
        default='draft'
    )
    
    # Generation metadata
    generation_metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'labor_employment_eeoc_packet'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'complaint_type']),
        ]

    def __str__(self):
        return f"EEOC Packet: {self.packet_name} - {self.complainant_name}"


class CommunicationPattern(models.Model):
    """
    Identifies patterns in workplace communications that may indicate issues.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_communication_patterns')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_communication_patterns')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='communication_patterns', null=True, blank=True)
    
    pattern_type = models.CharField(
        max_length=100,
        choices=[
            ('exclusion', 'Communication Exclusion'),
            ('timing', 'Unusual Timing Patterns'),
            ('sentiment_shift', 'Sentiment Shift'),
            ('volume_change', 'Communication Volume Change'),
            ('language_pattern', 'Language Pattern'),
            ('network_isolation', 'Network Isolation'),
            ('other', 'Other Pattern')
        ]
    )
    
    pattern_name = models.CharField(max_length=255, help_text="Descriptive name for the pattern")
    description = models.TextField(help_text="Detailed description of the pattern")
    
    # Pattern participants
    involved_personnel = models.JSONField(default=list, help_text="Personnel involved in the pattern")
    
    # Pattern metrics
    confidence_score = models.FloatField(help_text="Confidence in pattern detection (0.0 to 1.0)")
    severity_score = models.FloatField(help_text="Severity of the pattern (0.0 to 1.0)")
    
    # Time range
    pattern_start_date = models.DateTimeField()
    pattern_end_date = models.DateTimeField()
    
    # Supporting evidence
    supporting_messages = models.ManyToManyField(CommunicationMessage, related_name='communication_patterns')
    
    # Analysis details
    pattern_details = models.JSONField(default=dict, help_text="Detailed pattern analysis")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_communication_pattern'
        ordering = ['-severity_score', '-confidence_score']
        indexes = [
            models.Index(fields=['client', '-severity_score']),
            models.Index(fields=['client', 'pattern_type']),
        ]

    def __str__(self):
        return f"Pattern: {self.pattern_name} ({self.get_pattern_type_display()})"


class ComplianceAlert(models.Model):
    """
    Automated alerts for potential compliance issues found in communications.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_compliance_alerts')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_compliance_alerts')
    communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='compliance_alerts', null=True, blank=True)
    
    alert_type = models.CharField(
        max_length=100,
        choices=[
            ('harassment_language', 'Harassment Language Detected'),
            ('discriminatory_language', 'Discriminatory Language'),
            ('policy_violation', 'Policy Violation'),
            ('overtime_indication', 'Overtime Work Indication'),
            ('retaliation_risk', 'Potential Retaliation'),
            ('hostile_environment', 'Hostile Work Environment'),
            ('other', 'Other Compliance Issue')
        ]
    )
    
    alert_title = models.CharField(max_length=255)
    alert_description = models.TextField()
    
    # Severity and priority
    severity = models.CharField(
        max_length=20,
        choices=[
            ('low', 'Low'),
            ('medium', 'Medium'),
            ('high', 'High'),
            ('critical', 'Critical')
        ]
    )
    
    priority = models.CharField(
        max_length=20,
        choices=[
            ('low', 'Low Priority'),
            ('medium', 'Medium Priority'),
            ('high', 'High Priority'),
            ('urgent', 'Urgent')
        ]
    )
    
    # Related evidence
    related_messages = models.ManyToManyField(CommunicationMessage, related_name='compliance_alerts')
    
    # Alert status
    status = models.CharField(
        max_length=50,
        choices=[
            ('open', 'Open'),
            ('investigating', 'Under Investigation'),
            ('resolved', 'Resolved'),
            ('false_positive', 'False Positive'),
            ('dismissed', 'Dismissed')
        ],
        default='open'
    )
    
    # Resolution
    resolution_notes = models.TextField(blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='le_resolved_alerts')
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_compliance_alert'
        ordering = ['-severity', '-priority', '-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'alert_type', 'status']),
        ]

    def __str__(self):
        return f"Alert: {self.alert_title} ({self.get_severity_display()})"


# ═══════════════════════════════════════════════════════════════
# 🔄 COMPREHENSIVE SERVICE OUTPUT MODELS
# ═══════════════════════════════════════════════════════════════

class ServiceExecution(models.Model):
    """
    Tracks execution of all Labor Employment services with comprehensive metadata.
    """
    SERVICE_TYPES = [
        # Core Labor Employment Services
        ('le-create-communications-run', 'Create Workplace Communications Run'),
        ('le-sentiment-analysis', 'Communication Sentiment Analysis'),
        ('le-policy-compliance', 'Policy Compliance Check'),
        ('le-timeline-reconstruction', 'Timeline Reconstruction'),
        ('le-compliance-dashboard', 'Compliance Dashboard'),

        # AI-Enhanced Labor Employment Services
        ('le-hr-search', 'HR Document Search'),
        ('le-document-qa', 'Employment Q&A System'),
        ('le-structure-analysis', 'HR Document Structure Analysis'),
        ('le-file-insights', 'Employment File Insights'),
        ('le-document-anonymization', 'HR Document Anonymization'),
        ('le-ocr-employment', 'Employment Document OCR'),
        ('le-file-management', 'HR File Management'),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_service_executions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='le_service_executions')
    workplace_communications_run = models.ForeignKey(WorkplaceCommunicationsRun, on_delete=models.CASCADE, related_name='service_executions', null=True, blank=True)

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
    input_files = models.ManyToManyField('core.File', blank=True, related_name='le_service_inputs')
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
        db_table = 'labor_employment_service_execution'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['client', '-started_at']),
            models.Index(fields=['client', 'service_type']),
            models.Index(fields=['user', 'service_type']),
            models.Index(fields=['workplace_communications_run', 'status']),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='le_service_outputs')
    service_execution = models.ForeignKey(ServiceExecution, on_delete=models.CASCADE, related_name='outputs', null=True, blank=True)

    # Output details
    output_name = models.CharField(max_length=255, help_text="Name/title of the output")
    output_type = models.CharField(max_length=20, choices=ServiceExecution.OUTPUT_TYPES)
    file_extension = models.CharField(max_length=10, blank=True, help_text="File extension if applicable")
    mime_type = models.CharField(max_length=100, blank=True)

    # File storage
    output_file = models.FileField(upload_to='le_service_outputs/%Y/%m/%d/', null=True, blank=True)
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
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'labor_employment_service_output'
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
