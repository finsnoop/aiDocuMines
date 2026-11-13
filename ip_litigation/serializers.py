from rest_framework import serializers
from django.contrib.auth import get_user_model
from core.models import Run, File
from core.utils import get_user_client
from custom_authentication.models import Client
from .models import (
    PatentAnalysisRun, PatentDocument, PatentClaim, PriorArtDocument,
    ClaimChart, PatentLandscape, InfringementAnalysis, ValidityChallenge
)

User = get_user_model()


class PatentAnalysisRunSerializer(serializers.ModelSerializer):
    """
    Serializer for PatentAnalysisRun model (read operations).
    """
    run_id = serializers.UUIDField(source='run.run_id', read_only=True)
    user_email = serializers.EmailField(source='run.user.email', read_only=True)
    # Expose the UUID for external API use
    analysis_run_uuid = serializers.UUIDField(read_only=True)

    class Meta:
        model = PatentAnalysisRun
        fields = [
            'id', 'analysis_run_uuid', 'run_id', 'user_email', 'case_name', 'litigation_type', 'patent_sources',
            'patents_in_suit', 'technology_area', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'analysis_run_uuid', 'run_id', 'user_email', 'created_at', 'updated_at']


class PatentAnalysisRunCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating PatentAnalysisRun.
    Creates the core Run internally and accepts frontend field names.
    """
    # Frontend field aliases (write_only)
    patent_number = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Patent number(s) - comma separated")
    patent_title = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Patent title")
    analysis_type = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Analysis type - maps to litigation_type")
    patent_assignee = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Patent assignee/owner")
    opposing_party = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Opposing party name")
    key_claims = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Key claims to analyze")
    court_jurisdiction = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Court/venue")
    case_stage = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Current case stage")
    priority_date = serializers.DateField(write_only=True, required=False, allow_null=True, help_text="Patent priority date")
    deadline = serializers.DateField(write_only=True, required=False, allow_null=True, help_text="Key deadline")
    notes = serializers.CharField(write_only=True, required=False, allow_blank=True, help_text="Additional notes")
    case_name = serializers.CharField(required=False, default='Auto-created Patent Analysis')

    class Meta:
        model = PatentAnalysisRun
        fields = [
            'case_name', 'litigation_type', 'patent_sources', 'patents_in_suit', 'technology_area',
            # Frontend field aliases
            'patent_number', 'patent_title', 'analysis_type', 'patent_assignee',
            'opposing_party', 'key_claims', 'court_jurisdiction', 'case_stage',
            'priority_date', 'deadline', 'notes'
        ]

    def to_internal_value(self, data):
        """Transform frontend field names to backend field names before validation."""
        data = data.copy() if hasattr(data, 'copy') else dict(data)

        # Map analysis_type to litigation_type
        if 'analysis_type' in data and 'litigation_type' not in data:
            analysis_type_map = {
                'infringement': 'patent_infringement',
                'validity': 'validity_challenge',
                'prior_art': 'patent_infringement',
                'claim_construction': 'patent_infringement',
                'fto': 'licensing_dispute',
                'landscape': 'other',
            }
            data['litigation_type'] = analysis_type_map.get(data.get('analysis_type'), 'patent_infringement')

        # Map patent_number to patents_in_suit (convert comma-separated to list)
        if 'patent_number' in data and 'patents_in_suit' not in data:
            patent_numbers = data.get('patent_number', '')
            if isinstance(patent_numbers, str) and patent_numbers.strip():
                data['patents_in_suit'] = [p.strip() for p in patent_numbers.split(',') if p.strip()]
            else:
                data['patents_in_suit'] = []

        # Initialize patent_sources if not provided
        if 'patent_sources' not in data:
            data['patent_sources'] = ['USPTO']  # Default to USPTO

        return super().to_internal_value(data)

    def create(self, validated_data):
        """Create a new Run and associated PatentAnalysisRun with client context"""
        user = self.context['request'].user

        # Remove frontend-only fields that don't exist in the model
        frontend_fields = ['patent_number', 'patent_title', 'analysis_type', 'patent_assignee',
                          'opposing_party', 'key_claims', 'court_jurisdiction', 'case_stage',
                          'priority_date', 'deadline', 'notes']

        # Store extra data for metadata
        extra_data = {}
        for field in frontend_fields:
            if field in validated_data:
                extra_data[field] = validated_data.pop(field)

        # Handle admin users without a client - get or create default client
        if not user.client:
            client, _ = Client.objects.get_or_create(
                name="System Admin",
                defaults={
                    "address": "System",
                    "industry": "Legal Tech",
                    "use_case": "Administrative Operations"
                }
            )
        else:
            client = user.client

        # Create the core Run first (like MassClaimsRunCreateSerializer)
        run = Run.objects.create(
            user=user,
            status='Uploaded'
        )

        # Create the PatentAnalysisRun with client for multi-tenancy
        case_name = validated_data.get('case_name', '')
        if not case_name or case_name == 'Auto-created Patent Analysis':
            import uuid as _uuid
            validated_data['case_name'] = f"IP Analysis - {str(_uuid.uuid4())[:8]}"
        analysis_run = PatentAnalysisRun.objects.create(
            run=run,
            client=client,
            **validated_data
        )

        return analysis_run

    def validate_patents_in_suit(self, value):
        """Validate patents in suit format"""
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Patents in suit must be a list")

        for patent in value:
            if not isinstance(patent, str) or len(patent.strip()) == 0:
                raise serializers.ValidationError("Each patent number must be a non-empty string")

        return value


class PatentDocumentSerializer(serializers.ModelSerializer):
    """
    Serializer for PatentDocument model.
    """
    file_id = serializers.IntegerField(write_only=True, help_text="ID of the File containing patent document", required=False, default=None)
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)

    # Make these fields optional - they can be auto-generated from the file
    patent_number = serializers.CharField(required=False, allow_blank=True, help_text="Patent number")
    title = serializers.CharField(required=False, allow_blank=True, help_text="Patent title")
    patent_office = serializers.CharField(required=False, allow_blank=True, help_text="Patent office (e.g., USPTO)")
    application_number = serializers.CharField(required=False, allow_blank=True, help_text="Application number")
    publication_number = serializers.CharField(required=False, allow_blank=True, help_text="Publication number")
    inventors = serializers.JSONField(required=False, default=list, help_text="List of inventors")
    assignees = serializers.JSONField(required=False, default=list, help_text="List of assignees")
    filing_date = serializers.DateField(required=False, allow_null=True, help_text="Filing date")
    publication_date = serializers.DateField(required=False, allow_null=True, help_text="Publication date")
    grant_date = serializers.DateField(required=False, allow_null=True, help_text="Grant date")
    expiration_date = serializers.DateField(required=False, allow_null=True, help_text="Expiration date")
    status = serializers.CharField(required=False, allow_blank=True, help_text="Patent status")
    ipc_classes = serializers.JSONField(required=False, default=list, help_text="IPC classes")
    cpc_classes = serializers.JSONField(required=False, default=list, help_text="CPC classes")
    abstract = serializers.CharField(required=False, allow_blank=True, help_text="Patent abstract")
    claims_text = serializers.CharField(required=False, allow_blank=True, help_text="Claims text")
    description_text = serializers.CharField(required=False, allow_blank=True, help_text="Description text")
    processing_metadata = serializers.JSONField(required=False, default=dict, help_text="Processing metadata")

    class Meta:
        model = PatentDocument
        fields = [
            'id', 'file_id', 'analysis_run_id', 'patent_number', 'application_number',
            'publication_number', 'patent_office', 'title', 'inventors', 'assignees',
            'filing_date', 'publication_date', 'grant_date', 'expiration_date',
            'status', 'ipc_classes', 'cpc_classes', 'abstract', 'claims_text',
            'description_text', 'processing_metadata', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        file_id = validated_data.pop('file_id', None)
        analysis_run_uuid = validated_data.pop('analysis_run_id', None)

        user = self.context['request'].user
        file_obj = File.objects.get(id=file_id, user=user) if file_id else None
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None

        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        # Auto-generate defaults for optional fields if not provided
        patent_number = validated_data.get('patent_number') or f"PENDING-{file_obj.id if file_obj else 'UNKNOWN'}"
        validated_data['patent_number'] = patent_number
        if not validated_data.get('title'):
            validated_data['title'] = file_obj.filename if file_obj else f"Patent Document"
        if not validated_data.get('patent_office'):
            validated_data['patent_office'] = 'USPTO'

        # Use get_or_create to handle duplicates gracefully (like AI Document Processing services)
        patent_doc, created = PatentDocument.objects.get_or_create(
            patent_number=patent_number,
            analysis_run=analysis_run,
            defaults={
                'client': client,
                'file': file_obj,
                'user': user,
                **validated_data
            }
        )

        # If document already exists, update it with new data
        if not created:
            for key, value in validated_data.items():
                setattr(patent_doc, key, value)
            patent_doc.file = file_obj  # Update the file reference
            patent_doc.save()

        return patent_doc

    def validate_patent_number(self, value):
        """Validate patent number format - now allows empty values"""
        if value:
            return value.strip().upper()
        return value


class PatentClaimSerializer(serializers.ModelSerializer):
    """
    Serializer for PatentClaim model.
    """
    patent_document_id = serializers.IntegerField(write_only=True, help_text="ID of the PatentDocument", required=False, default=None)
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)

    # Make these fields optional with defaults
    claim_number = serializers.IntegerField(required=False, default=1, help_text="Claim number")
    claim_text = serializers.CharField(required=False, allow_blank=True, help_text="Claim text")
    claim_type = serializers.CharField(required=False, allow_blank=True, help_text="Claim type (independent/dependent)")
    depends_on_claims = serializers.JSONField(required=False, default=list, help_text="Dependent claims list")
    claim_elements = serializers.JSONField(required=False, default=list, help_text="Claim elements")
    element_count = serializers.IntegerField(required=False, default=0, help_text="Element count")
    complexity_score = serializers.FloatField(required=False, allow_null=True, help_text="Complexity score")

    class Meta:
        model = PatentClaim
        fields = [
            'id', 'patent_document_id', 'analysis_run_id', 'claim_number', 'claim_text',
            'claim_type', 'depends_on_claims', 'claim_elements', 'element_count',
            'complexity_score', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        patent_document_id = validated_data.pop('patent_document_id', None)
        analysis_run_uuid = validated_data.pop('analysis_run_id', None)

        user = self.context['request'].user
        patent_document = PatentDocument.objects.get(id=patent_document_id, user=user) if patent_document_id else None
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None

        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        return PatentClaim.objects.create(
            client=client,
            patent_document=patent_document,
            user=user,
            analysis_run=analysis_run,
            **validated_data
        )

    def validate_claim_number(self, value):
        """Validate claim number is positive"""
        if value <= 0:
            raise serializers.ValidationError("Claim number must be positive")
        return value


class PriorArtDocumentSerializer(serializers.ModelSerializer):
    """
    Serializer for PriorArtDocument model.
    """
    file_id = serializers.IntegerField(write_only=True, help_text="ID of the File containing prior art document", required=False, default=None)
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)

    # Make these fields optional - they can be auto-generated from the file
    document_id = serializers.CharField(required=False, allow_blank=True, help_text="Unique document identifier")
    document_type = serializers.CharField(required=False, allow_blank=True, help_text="Type of prior art document")
    title = serializers.CharField(required=False, allow_blank=True, help_text="Document title")
    authors = serializers.JSONField(required=False, default=list, help_text="Document authors")
    publication_date = serializers.DateField(required=False, allow_null=True, help_text="Publication date")
    source = serializers.CharField(required=False, allow_blank=True, help_text="Document source")
    abstract = serializers.CharField(required=False, allow_blank=True, help_text="Document abstract")
    content_text = serializers.CharField(required=False, allow_blank=True, help_text="Full content text")
    relevance_score = serializers.FloatField(required=False, allow_null=True, help_text="Relevance score 0-1")
    relevance_explanation = serializers.CharField(required=False, allow_blank=True, help_text="Relevance explanation")
    art_categories = serializers.JSONField(required=False, default=list, help_text="Art categories")

    class Meta:
        model = PriorArtDocument
        fields = [
            'id', 'file_id', 'analysis_run_id', 'document_id', 'document_type',
            'title', 'authors', 'publication_date', 'source', 'abstract',
            'content_text', 'relevance_score', 'relevance_explanation',
            'art_categories', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        file_id = validated_data.pop('file_id', None)
        analysis_run_uuid = validated_data.pop('analysis_run_id', None)

        user = self.context['request'].user
        file_obj = File.objects.get(id=file_id, user=user) if file_id else None
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None

        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        # Auto-generate defaults for optional fields if not provided
        document_id = validated_data.get('document_id') or f"PA-{file_obj.id if file_obj else '0'}-{analysis_run.id if analysis_run else '0'}"
        validated_data['document_id'] = document_id
        if not validated_data.get('document_type'):
            validated_data['document_type'] = 'prior_art'
        if not validated_data.get('title'):
            validated_data['title'] = file_obj.filename if file_obj else "Prior Art Document"

        # Use get_or_create to handle duplicates gracefully
        prior_art_doc, created = PriorArtDocument.objects.get_or_create(
            document_id=document_id,
            analysis_run=analysis_run,
            defaults={
                'client': client,
                'file': file_obj,
                'user': user,
                **validated_data
            }
        )

        # If document already exists, update it with new data
        if not created:
            for key, value in validated_data.items():
                setattr(prior_art_doc, key, value)
            prior_art_doc.file = file_obj
            prior_art_doc.save()

        return prior_art_doc

    def validate_relevance_score(self, value):
        """Validate relevance score is between 0 and 1"""
        if value is not None and (value < 0.0 or value > 1.0):
            raise serializers.ValidationError("Relevance score must be between 0.0 and 1.0")
        return value


class ClaimChartSerializer(serializers.ModelSerializer):
    """
    Serializer for ClaimChart model.
    """
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)
    patent_claim_id = serializers.IntegerField(write_only=True, help_text="ID of the PatentClaim", required=False, default=None)
    target_prior_art_id = serializers.IntegerField(write_only=True, required=False, help_text="ID of the PriorArtDocument")
    supporting_document_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
        help_text="List of File IDs for supporting documents"
    )

    # Make these fields optional with defaults
    chart_name = serializers.CharField(required=False, allow_blank=True, help_text="Name for this chart")
    chart_type = serializers.CharField(required=False, allow_blank=True, help_text="Chart type")
    target_product = serializers.CharField(required=False, allow_blank=True, help_text="Target product")
    element_mappings = serializers.JSONField(required=False, default=list, help_text="Element mappings")
    overall_conclusion = serializers.CharField(required=False, allow_blank=True, help_text="Overall conclusion")
    confidence_score = serializers.FloatField(required=False, allow_null=True, help_text="Confidence score 0-1")
    analysis_notes = serializers.CharField(required=False, allow_blank=True, help_text="Analysis notes")

    class Meta:
        model = ClaimChart
        fields = [
            'id', 'analysis_run_id', 'patent_claim_id', 'chart_name', 'chart_type',
            'target_product', 'target_prior_art_id', 'element_mappings',
            'overall_conclusion', 'confidence_score', 'analysis_notes',
            'supporting_document_ids', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        analysis_run_id = validated_data.pop('analysis_run_id', None)
        patent_claim_id = validated_data.pop('patent_claim_id', None)
        target_prior_art_id = validated_data.pop('target_prior_art_id', None)
        supporting_document_ids = validated_data.pop('supporting_document_ids', [])

        user = self.context['request'].user
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_id, run__user=user) if analysis_run_id else None
        patent_claim = PatentClaim.objects.get(id=patent_claim_id, user=user) if patent_claim_id else None

        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        target_prior_art = None
        if target_prior_art_id:
            target_prior_art = PriorArtDocument.objects.get(id=target_prior_art_id, user=user)

        # Use get_or_create to handle duplicates gracefully
        claim_chart, created = ClaimChart.objects.get_or_create(
            analysis_run=analysis_run,
            patent_claim=patent_claim,
            target_prior_art=target_prior_art,
            user=user,
            defaults={
                'client': client,
                **validated_data
            }
        )

        # If already exists, update with new data
        if not created:
            for key, value in validated_data.items():
                setattr(claim_chart, key, value)
            claim_chart.save()

        # Add supporting documents
        if supporting_document_ids:
            supporting_docs = File.objects.filter(id__in=supporting_document_ids, user=user)
            claim_chart.supporting_documents.set(supporting_docs)

        return claim_chart

    def validate_confidence_score(self, value):
        """Validate confidence score is between 0 and 1"""
        if value is not None and (value < 0.0 or value > 1.0):
            raise serializers.ValidationError("Confidence score must be between 0.0 and 1.0")
        return value


class PatentLandscapeSerializer(serializers.ModelSerializer):
    """
    Serializer for PatentLandscape model.
    """
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun")

    class Meta:
        model = PatentLandscape
        fields = [
            'id', 'analysis_run_id', 'landscape_name', 'technology_area',
            'search_keywords', 'classification_codes', 'date_range_start',
            'date_range_end', 'total_patents_found', 'key_players',
            'technology_trends', 'patent_clusters', 'landscape_metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        analysis_run_uuid = validated_data.pop('analysis_run_id')  # This is now a UUID

        user = self.context['request'].user
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None

        # Get client from user or analysis_run for multi-tenancy
        client = getattr(user, 'client', None) or analysis_run.client

        return PatentLandscape.objects.create(
            client=client,
            user=user,
            analysis_run=analysis_run,
            **validated_data
        )



class InfringementAnalysisSerializer(serializers.ModelSerializer):
    """
    Serializer for InfringementAnalysis model.
    """
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)
    asserted_patent_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,  # Make optional - can be empty initially
        default=list,
        help_text="List of PatentDocument IDs being asserted"
    )

    # Make these fields optional with defaults - match model field types
    analysis_name = serializers.CharField(required=False, allow_blank=True, help_text="Name for this analysis")
    accused_product = serializers.CharField(required=False, allow_blank=True, help_text="Accused product/process")
    analysis_methodology = serializers.CharField(required=False, allow_blank=True, default='', help_text="Analysis methodology")
    literal_infringement = serializers.CharField(required=False, allow_blank=True, default='unclear', help_text="Literal infringement: yes, no, partial, unclear")
    doctrine_of_equivalents = serializers.CharField(required=False, allow_blank=True, default='not_applicable', help_text="DOE: yes, no, possible, not_applicable")
    infringement_conclusion = serializers.CharField(required=False, allow_blank=True, default='inconclusive', help_text="Conclusion: infringes, does_not_infringe, mixed, inconclusive")
    confidence_level = serializers.ChoiceField(
        choices=[('high', 'High'), ('medium', 'Medium'), ('low', 'Low')],
        required=False, default='medium', help_text="Confidence level: high, medium, low"
    )
    detailed_findings = serializers.JSONField(required=False, default=dict, help_text="Detailed findings")
    recommendations = serializers.JSONField(required=False, default=list, help_text="Recommendations list")

    class Meta:
        model = InfringementAnalysis
        fields = [
            'id', 'analysis_run_id', 'analysis_name', 'accused_product',
            'asserted_patent_ids', 'analysis_methodology', 'literal_infringement',
            'doctrine_of_equivalents', 'infringement_conclusion', 'confidence_level',
            'detailed_findings', 'recommendations', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        analysis_run_uuid = validated_data.pop('analysis_run_id')  # This is now a UUID
        asserted_patent_ids = validated_data.pop('asserted_patent_ids', [])

        user = self.context['request'].user
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None

        # Get client from user or analysis_run for multi-tenancy
        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        # Auto-generate defaults for optional fields if not provided
        analysis_name = validated_data.get('analysis_name') or f"Infringement Analysis - {analysis_run.case_name if analysis_run else 'Unknown'}"
        validated_data['analysis_name'] = analysis_name
        if not validated_data.get('accused_product'):
            validated_data['accused_product'] = 'Not specified'

        # Use get_or_create to handle duplicates gracefully
        infringement_analysis, created = InfringementAnalysis.objects.get_or_create(
            analysis_name=analysis_name,
            analysis_run=analysis_run,
            user=user,
            defaults={
                'client': client,
                **validated_data
            }
        )

        # If already exists, update with new data
        if not created:
            for key, value in validated_data.items():
                setattr(infringement_analysis, key, value)
            infringement_analysis.save()

        # Add asserted patents if provided
        if asserted_patent_ids:
            asserted_patents = PatentDocument.objects.filter(id__in=asserted_patent_ids, user=user)
            infringement_analysis.asserted_patents.set(asserted_patents)

        return infringement_analysis


class ValidityChallengeSerializer(serializers.ModelSerializer):
    """
    Serializer for ValidityChallenge model.
    """
    analysis_run_id = serializers.UUIDField(write_only=True, help_text="UUID of the PatentAnalysisRun", required=False, default=None)
    target_patent_id = serializers.IntegerField(write_only=True, help_text="ID of the target PatentDocument", required=False, default=None)
    prior_art_reference_ids = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,  # Make optional
        default=list,
        help_text="List of PriorArtDocument IDs"
    )

    # Make these fields optional with defaults
    challenge_name = serializers.CharField(required=False, allow_blank=True, help_text="Name for this challenge")
    challenge_grounds = serializers.JSONField(required=False, default=list, help_text="Challenge grounds")
    anticipation_analysis = serializers.JSONField(required=False, default=dict, help_text="Anticipation analysis")
    obviousness_analysis = serializers.JSONField(required=False, default=dict, help_text="Obviousness analysis")
    challenge_strength = serializers.CharField(required=False, allow_blank=True, help_text="Challenge strength")
    success_likelihood = serializers.FloatField(required=False, allow_null=True, help_text="Success likelihood 0-1")
    detailed_analysis = serializers.JSONField(required=False, default=dict, help_text="Detailed analysis")

    class Meta:
        model = ValidityChallenge
        fields = [
            'id', 'analysis_run_id', 'target_patent_id', 'challenge_name',
            'challenge_grounds', 'prior_art_reference_ids', 'anticipation_analysis',
            'obviousness_analysis', 'challenge_strength', 'success_likelihood',
            'detailed_analysis', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        analysis_run_uuid = validated_data.pop('analysis_run_id', None)
        target_patent_id = validated_data.pop('target_patent_id', None)
        prior_art_reference_ids = validated_data.pop('prior_art_reference_ids', [])

        user = self.context['request'].user
        analysis_run = PatentAnalysisRun.objects.get(analysis_run_uuid=analysis_run_uuid, run__user=user) if analysis_run_uuid else None
        target_patent = PatentDocument.objects.get(id=target_patent_id, user=user) if target_patent_id else None

        client = getattr(user, 'client', None) or (analysis_run.client if analysis_run else None)

        # Auto-generate defaults for optional fields if not provided
        challenge_name = validated_data.get('challenge_name') or f"Validity Challenge - {target_patent.title if target_patent else 'Unknown'}"
        validated_data['challenge_name'] = challenge_name

        # Use get_or_create to handle duplicates gracefully
        validity_challenge, created = ValidityChallenge.objects.get_or_create(
            analysis_run=analysis_run,
            target_patent=target_patent,
            challenge_name=challenge_name,
            user=user,
            defaults={
                'client': client,
                **validated_data
            }
        )

        # If already exists, update with new data
        if not created:
            for key, value in validated_data.items():
                setattr(validity_challenge, key, value)
            validity_challenge.save()

        # Add prior art references if provided
        if prior_art_reference_ids:
            prior_art_refs = PriorArtDocument.objects.filter(id__in=prior_art_reference_ids, user=user)
            validity_challenge.prior_art_references.set(prior_art_refs)

        return validity_challenge

    def validate_success_likelihood(self, value):
        """Validate success likelihood is between 0 and 1"""
        if value is not None and (value < 0.0 or value > 1.0):
            raise serializers.ValidationError("Success likelihood must be between 0.0 and 1.0")
        return value


# Summary serializers for analytics
class PatentAnalysisSummarySerializer(serializers.Serializer):
    """
    Summary serializer for patent analysis statistics.
    """
    total_patents = serializers.IntegerField()
    total_claims = serializers.IntegerField()
    total_prior_art = serializers.IntegerField()
    total_claim_charts = serializers.IntegerField()
    infringement_analyses = serializers.IntegerField()
    validity_challenges = serializers.IntegerField()

    patent_office_breakdown = serializers.DictField()
    litigation_type_breakdown = serializers.DictField()
    technology_area_breakdown = serializers.DictField()


class ClaimChartSummarySerializer(serializers.Serializer):
    """
    Summary serializer for claim chart statistics.
    """
    chart_type = serializers.CharField()
    chart_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    infringes_count = serializers.IntegerField()
    does_not_infringe_count = serializers.IntegerField()
    unclear_count = serializers.IntegerField()
    avg_confidence_score = serializers.FloatField()


class InfringementSummarySerializer(serializers.Serializer):
    """
    Summary serializer for infringement analysis statistics.
    """
    total_analyses = serializers.IntegerField()
    infringement_found = serializers.IntegerField()
    no_infringement = serializers.IntegerField()
    mixed_results = serializers.IntegerField()
    inconclusive = serializers.IntegerField()

    literal_infringement_rate = serializers.FloatField()
    doe_infringement_rate = serializers.FloatField()
    high_confidence_analyses = serializers.IntegerField()


class ValiditySummarySerializer(serializers.Serializer):
    """
    Summary serializer for validity challenge statistics.
    """
    total_challenges = serializers.IntegerField()
    strong_challenges = serializers.IntegerField()
    moderate_challenges = serializers.IntegerField()
    weak_challenges = serializers.IntegerField()

    avg_success_likelihood = serializers.FloatField()
    anticipation_challenges = serializers.IntegerField()
    obviousness_challenges = serializers.IntegerField()
    most_challenged_patents = serializers.ListField()
