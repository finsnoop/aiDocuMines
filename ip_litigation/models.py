from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import json

User = get_user_model()


class PatentAnalysisRun(models.Model):
    """
    Represents a patent analysis run for IP litigation matters.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_patent_analysis_runs')
    run = models.OneToOneField('core.Run', on_delete=models.CASCADE, related_name='patent_analysis')

    # Auto-generated UUID for external references (API, frontend)
    analysis_run_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, help_text="Unique identifier for this analysis run")

    case_name = models.CharField(max_length=255, help_text="Name of the IP litigation case")

    litigation_type = models.CharField(
        max_length=100,
        choices=[
            ('patent_infringement', 'Patent Infringement'),
            ('trademark_dispute', 'Trademark Dispute'),
            ('copyright_infringement', 'Copyright Infringement'),
            ('trade_secret', 'Trade Secret'),
            ('licensing_dispute', 'Licensing Dispute'),
            ('validity_challenge', 'Patent Validity Challenge'),
            ('other', 'Other IP Dispute')
        ],
        default='patent_infringement'
    )

    # Patent office sources
    patent_sources = models.JSONField(
        default=list,
        help_text="List of patent office sources (USPTO, EPO, JPO, etc.)"
    )

    # Key patents in dispute
    patents_in_suit = models.JSONField(
        default=list,
        help_text="List of patent numbers in the litigation"
    )

    # Technology area
    technology_area = models.CharField(
        max_length=255,
        blank=True,
        help_text="Technology area or field of the patents"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_patent_analysis_run'
        ordering = ['-created_at']
        # Prevent duplicate case names within the same client
        unique_together = ['case_name', 'client']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'litigation_type']),
        ]

    def __str__(self):
        return f"{self.case_name} - {self.get_litigation_type_display()}"


class PatentDocument(models.Model):
    """
    Represents patent documents from various patent offices.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_patent_documents')
    file = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='ip_patent_documents', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_patent_documents')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='patent_documents', null=True, blank=True)

    # Patent identification
    patent_number = models.CharField(max_length=100, help_text="Patent number (e.g., US10123456B2)")
    application_number = models.CharField(max_length=100, blank=True, help_text="Patent application number")
    publication_number = models.CharField(max_length=100, blank=True, help_text="Publication number")

    # Patent office and jurisdiction
    patent_office = models.CharField(
        max_length=50,
        choices=[
            ('uspto', 'USPTO (United States)'),
            ('epo', 'EPO (European Patent Office)'),
            ('jpo', 'JPO (Japan Patent Office)'),
            ('cipo', 'CIPO (Canada)'),
            ('cnipa', 'CNIPA (China)'),
            ('kipo', 'KIPO (South Korea)'),
            ('other', 'Other Patent Office')
        ]
    )

    # Patent metadata
    title = models.CharField(max_length=500, help_text="Patent title")
    inventors = models.JSONField(default=list, help_text="List of inventors")
    assignees = models.JSONField(default=list, help_text="List of assignees/owners")

    # Dates
    filing_date = models.DateField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    grant_date = models.DateField(null=True, blank=True)
    expiration_date = models.DateField(null=True, blank=True)

    # Patent status
    status = models.CharField(
        max_length=50,
        choices=[
            ('pending', 'Pending'),
            ('granted', 'Granted'),
            ('expired', 'Expired'),
            ('abandoned', 'Abandoned'),
            ('rejected', 'Rejected')
        ],
        default='pending'
    )

    # Classification
    ipc_classes = models.JSONField(default=list, help_text="International Patent Classification codes")
    cpc_classes = models.JSONField(default=list, help_text="Cooperative Patent Classification codes")

    # Content extraction
    abstract = models.TextField(blank=True, help_text="Patent abstract")
    claims_text = models.TextField(blank=True, help_text="Patent claims text")
    description_text = models.TextField(blank=True, help_text="Patent description text")

    # Processing metadata
    processing_metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_patent_document'
        ordering = ['-filing_date']
        unique_together = ['patent_number', 'analysis_run']
        indexes = [
            models.Index(fields=['client', '-filing_date']),
            models.Index(fields=['client', 'patent_number']),
            models.Index(fields=['client', 'status']),
        ]

    def __str__(self):
        return f"{self.patent_number} - {self.title[:50]}..."


class PatentClaim(models.Model):
    """
    Individual patent claims extracted from patent documents.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_patent_claims')
    patent_document = models.ForeignKey(PatentDocument, on_delete=models.CASCADE, related_name='claims', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_patent_claims')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='patent_claims', null=True, blank=True)

    claim_number = models.IntegerField(help_text="Claim number within the patent")
    claim_text = models.TextField(help_text="Full text of the patent claim")

    # Claim type
    claim_type = models.CharField(
        max_length=50,
        choices=[
            ('independent', 'Independent Claim'),
            ('dependent', 'Dependent Claim'),
            ('method', 'Method Claim'),
            ('apparatus', 'Apparatus Claim'),
            ('system', 'System Claim'),
            ('composition', 'Composition Claim')
        ]
    )

    # Dependency relationships
    depends_on_claims = models.JSONField(default=list, help_text="List of claim numbers this claim depends on")

    # Claim elements (parsed components)
    claim_elements = models.JSONField(default=list, help_text="Parsed claim elements/limitations")

    # Analysis results
    element_count = models.IntegerField(default=0, help_text="Number of claim elements")
    complexity_score = models.FloatField(null=True, blank=True, help_text="Claim complexity score (0.0-1.0)")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_patent_claim'
        ordering = ['claim_number']
        unique_together = ['patent_document', 'claim_number']
        indexes = [
            models.Index(fields=['client', 'claim_number']),
            models.Index(fields=['client', 'claim_type']),
        ]

    def __str__(self):
        return f"Claim {self.claim_number} - {self.patent_document.patent_number}"


class PriorArtDocument(models.Model):
    """
    Prior art documents for patent analysis and invalidity challenges.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_prior_art_documents')
    file = models.ForeignKey('core.File', on_delete=models.CASCADE, related_name='ip_prior_art_documents', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_prior_art_documents')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='prior_art_documents', null=True, blank=True)

    # Document identification
    document_id = models.CharField(max_length=255, help_text="Document identifier (patent number, publication ID, etc.)")
    document_type = models.CharField(
        max_length=50,
        choices=[
            ('patent', 'Patent Document'),
            ('publication', 'Scientific Publication'),
            ('standard', 'Technical Standard'),
            ('product_manual', 'Product Manual'),
            ('website', 'Website/Online Content'),
            ('other', 'Other Document')
        ]
    )

    # Document metadata
    title = models.CharField(max_length=500)
    authors = models.JSONField(default=list, help_text="List of authors")
    publication_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=255, blank=True, help_text="Publication source or venue")

    # Content
    abstract = models.TextField(blank=True)
    content_text = models.TextField(blank=True, help_text="Extracted document content")

    # Relevance analysis
    relevance_score = models.FloatField(null=True, blank=True, help_text="Relevance to patents in suit (0.0-1.0)")
    relevance_explanation = models.TextField(blank=True, help_text="Explanation of relevance")

    # Prior art categories
    art_categories = models.JSONField(default=list, help_text="Categories of prior art disclosed")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_prior_art_document'
        ordering = ['-relevance_score', '-publication_date']
        indexes = [
            models.Index(fields=['client', '-relevance_score']),
            models.Index(fields=['client', '-publication_date']),
            models.Index(fields=['client', 'document_type']),
        ]

    def __str__(self):
        return f"{self.document_id} - {self.title[:50]}..."


class ClaimChart(models.Model):
    """
    Claim charts mapping patent claims to accused products or prior art.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_claim_charts')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_claim_charts')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='claim_charts', null=True, blank=True)
    patent_claim = models.ForeignKey(PatentClaim, on_delete=models.CASCADE, related_name='claim_charts', null=True, blank=True)

    chart_name = models.CharField(max_length=255, help_text="Name of the claim chart")
    chart_type = models.CharField(
        max_length=50,
        choices=[
            ('infringement', 'Infringement Analysis'),
            ('invalidity', 'Invalidity Analysis'),
            ('non_infringement', 'Non-Infringement Analysis')
        ]
    )

    # Target of analysis
    target_product = models.CharField(max_length=255, blank=True, help_text="Accused product or system")
    target_prior_art = models.ForeignKey(PriorArtDocument, on_delete=models.SET_NULL, null=True, blank=True, related_name='claim_charts')

    # Claim chart mappings
    element_mappings = models.JSONField(default=list, help_text="Mappings of claim elements to product features or prior art")

    # Analysis results
    overall_conclusion = models.CharField(
        max_length=50,
        choices=[
            ('infringes', 'Infringes'),
            ('does_not_infringe', 'Does Not Infringe'),
            ('invalid', 'Invalid'),
            ('valid', 'Valid'),
            ('unclear', 'Unclear/Needs Analysis')
        ],
        default='unclear'
    )

    confidence_score = models.FloatField(null=True, blank=True, help_text="Confidence in analysis (0.0-1.0)")
    analysis_notes = models.TextField(blank=True, help_text="Detailed analysis notes")

    # Supporting evidence
    supporting_documents = models.ManyToManyField('core.File', related_name='ip_claim_charts', blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_claim_chart'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'chart_type']),
        ]

    def __str__(self):
        return f"{self.chart_name} - {self.patent_claim}"


class PatentLandscape(models.Model):
    """
    Patent landscape analysis for technology areas.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_patent_landscapes')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_patent_landscapes')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='patent_landscapes', null=True, blank=True)

    landscape_name = models.CharField(max_length=255, help_text="Name of the patent landscape")
    technology_area = models.CharField(max_length=255, help_text="Technology area being analyzed")

    # Search parameters
    search_keywords = models.JSONField(default=list, help_text="Keywords used for patent search")
    classification_codes = models.JSONField(default=list, help_text="Patent classification codes searched")
    date_range_start = models.DateField(null=True, blank=True)
    date_range_end = models.DateField(null=True, blank=True)

    # Analysis results
    total_patents_found = models.IntegerField(default=0)
    key_players = models.JSONField(default=list, help_text="Key patent holders/assignees")
    technology_trends = models.JSONField(default=list, help_text="Identified technology trends")

    # Patent clusters
    patent_clusters = models.JSONField(default=list, help_text="Clustered patent groups by technology")

    # Landscape metadata
    landscape_metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_patent_landscape'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'technology_area']),
        ]

    def __str__(self):
        return f"{self.landscape_name} - {self.technology_area}"


class InfringementAnalysis(models.Model):
    """
    Comprehensive infringement analysis for patent litigation.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_infringement_analyses')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_infringement_analyses')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='infringement_analyses', null=True, blank=True)

    analysis_name = models.CharField(max_length=255, help_text="Name of the infringement analysis")
    accused_product = models.CharField(max_length=255, help_text="Product or system accused of infringement")

    # Patents being asserted
    asserted_patents = models.ManyToManyField(PatentDocument, related_name='infringement_analyses')

    # Analysis methodology
    analysis_methodology = models.TextField(help_text="Description of analysis methodology")

    # Infringement findings
    literal_infringement = models.CharField(
        max_length=50,
        choices=[
            ('yes', 'Yes - Literal Infringement'),
            ('no', 'No - No Literal Infringement'),
            ('partial', 'Partial - Some Claims'),
            ('unclear', 'Unclear/Needs Further Analysis')
        ],
        default='unclear'
    )

    doctrine_of_equivalents = models.CharField(
        max_length=50,
        choices=[
            ('yes', 'Yes - Infringement Under DOE'),
            ('no', 'No - No DOE Infringement'),
            ('possible', 'Possible - Needs Analysis'),
            ('not_applicable', 'Not Applicable')
        ],
        default='not_applicable'
    )

    # Overall conclusion
    infringement_conclusion = models.CharField(
        max_length=50,
        choices=[
            ('infringes', 'Infringes'),
            ('does_not_infringe', 'Does Not Infringe'),
            ('mixed', 'Mixed Results'),
            ('inconclusive', 'Inconclusive')
        ],
        default='inconclusive'
    )

    confidence_level = models.CharField(
        max_length=20,
        choices=[
            ('high', 'High Confidence'),
            ('medium', 'Medium Confidence'),
            ('low', 'Low Confidence')
        ],
        default='medium'
    )

    # Analysis details
    detailed_findings = models.JSONField(default=dict, help_text="Detailed infringement findings")
    recommendations = models.JSONField(default=list, help_text="Recommendations based on analysis")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_infringement_analysis'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'infringement_conclusion']),
            models.Index(fields=['client', 'confidence_level']),
        ]

    def __str__(self):
        return f"{self.analysis_name} - {self.accused_product}"


class ValidityChallenge(models.Model):
    """
    Patent validity challenge analysis using prior art.
    Multi-tenant: Isolated by client.
    """
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_validity_challenges')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_validity_challenges')
    analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='validity_challenges', null=True, blank=True)
    target_patent = models.ForeignKey(PatentDocument, on_delete=models.CASCADE, related_name='validity_challenges', null=True, blank=True)

    challenge_name = models.CharField(max_length=255, help_text="Name of the validity challenge")

    # Challenge grounds
    challenge_grounds = models.JSONField(
        default=list,
        help_text="Grounds for invalidity (anticipation, obviousness, etc.)"
    )

    # Prior art references
    prior_art_references = models.ManyToManyField(PriorArtDocument, related_name='validity_challenges')

    # Invalidity analysis
    anticipation_analysis = models.TextField(blank=True, help_text="Anticipation analysis under 35 U.S.C. § 102")
    obviousness_analysis = models.TextField(blank=True, help_text="Obviousness analysis under 35 U.S.C. § 103")

    # Challenge strength
    challenge_strength = models.CharField(
        max_length=20,
        choices=[
            ('strong', 'Strong Challenge'),
            ('moderate', 'Moderate Challenge'),
            ('weak', 'Weak Challenge')
        ],
        default='moderate'
    )

    # Likelihood of success
    success_likelihood = models.FloatField(null=True, blank=True, help_text="Likelihood of success (0.0-1.0)")

    # Challenge details
    detailed_analysis = models.JSONField(default=dict, help_text="Detailed validity challenge analysis")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ip_litigation_validity_challenge'
        ordering = ['-success_likelihood', '-created_at']
        indexes = [
            models.Index(fields=['client', '-success_likelihood']),
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'challenge_strength']),
        ]

    def __str__(self):
        return f"{self.challenge_name} - {self.target_patent.patent_number}"


# ═══════════════════════════════════════════════════════════════
# 🔄 COMPREHENSIVE SERVICE OUTPUT MODELS
# ═══════════════════════════════════════════════════════════════

class ServiceExecution(models.Model):
    """
    Tracks execution of all IP Litigation services with comprehensive metadata.
    """
    SERVICE_TYPES = [
        # Core IP Litigation Services
        ('ip-create-patent-run', 'Create Patent Analysis Run'),
        ('ip-prior-art-search', 'Prior Art Search & Analysis'),
        ('ip-claim-construction', 'Claim Construction Analysis'),
        ('ip-infringement-analysis', 'Infringement Analysis'),
        ('ip-validity-challenge', 'Patent Validity Challenge'),

        # AI-Enhanced IP Litigation Services
        ('ip-semantic-search', 'Patent Semantic Search'),
        ('ip-document-qa', 'IP Document Q&A'),
        ('ip-structure-analysis', 'Patent Structure Analysis'),
        ('ip-file-insights', 'IP File Insights'),
        ('ip-document-comparison', 'Patent Document Comparison'),
        ('ip-ocr-technical', 'Technical Document OCR'),
        ('ip-technical-search', 'Technical Literature Search'),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_service_executions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ip_service_executions')
    patent_analysis_run = models.ForeignKey(PatentAnalysisRun, on_delete=models.CASCADE, related_name='service_executions', null=True, blank=True)

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
    input_files = models.ManyToManyField('core.File', blank=True, related_name='ip_service_inputs')
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
        db_table = 'ip_litigation_service_execution'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'service_type']),
            models.Index(fields=['patent_analysis_run', 'status']),
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
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ipl_service_outputs')
    service_execution = models.ForeignKey(ServiceExecution, on_delete=models.CASCADE, related_name='outputs', null=True, blank=True)

    # Output details
    output_name = models.CharField(max_length=255, help_text="Name/title of the output")
    output_type = models.CharField(max_length=20, choices=ServiceExecution.OUTPUT_TYPES)
    file_extension = models.CharField(max_length=10, blank=True, help_text="File extension if applicable")
    mime_type = models.CharField(max_length=100, blank=True)

    # File storage
    output_file = models.FileField(upload_to='ip_service_outputs/%Y/%m/%d/', null=True, blank=True)
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
        db_table = 'ip_litigation_service_output'
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
