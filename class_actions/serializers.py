from rest_framework import serializers
from django.contrib.auth import get_user_model
from core.models import File, Run
from core.utils import get_user_client
from custom_authentication.models import Client
from .models import (
    MassClaimsRun, IntakeForm, EvidenceDocument, PIIRedaction,
    ExhibitPackage, SettlementTracking, ClaimantCommunication
)

User = get_user_model()


class MassClaimsRunSerializer(serializers.ModelSerializer):
    """Serializer for MassClaimsRun model"""
    
    run_id = serializers.UUIDField(source='run.run_id', read_only=True)
    user_email = serializers.EmailField(source='run.user.email', read_only=True)
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    total_intake_forms = serializers.SerializerMethodField()
    total_evidence_docs = serializers.SerializerMethodField()
    
    class Meta:
        model = MassClaimsRun
        fields = [
            'id', 'run_id', 'user_email', 'case_name', 'case_number',
            'court_jurisdiction', 'case_type', 'case_type_display',
            'claim_deadline', 'settlement_amount', 'status', 'status_display',
            'total_intake_forms', 'total_evidence_docs', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_total_intake_forms(self, obj):
        """Get total number of intake forms"""
        return obj.intake_forms.count()
    
    def get_total_evidence_docs(self, obj):
        """Get total number of evidence documents"""
        return obj.evidence_documents.count()
    
    def validate_settlement_amount(self, value):
        """Validate settlement amount is positive"""
        if value is not None and value <= 0:
            raise serializers.ValidationError("Settlement amount must be positive")
        return value


class IntakeFormSerializer(serializers.ModelSerializer):
    """Serializer for IntakeForm model"""
    
    processing_status_display = serializers.CharField(source='get_processing_status_display', read_only=True)
    processed_by_email = serializers.EmailField(source='processed_by.email', read_only=True)
    duplicate_of_id = serializers.UUIDField(source='duplicate_of.claimant_id', read_only=True)
    
    class Meta:
        model = IntakeForm
        fields = [
            'id', 'claimant_id', 'mass_claims_run', 'claimant_data',
            'is_duplicate', 'duplicate_of_id', 'duplicate_score',
            'processing_status', 'processing_status_display',
            'is_valid', 'validation_errors', 'submitted_at',
            'processed_at', 'processed_by_email'
        ]
        read_only_fields = ['id', 'claimant_id', 'user', 'submitted_at', 'processed_at']
    
    def validate_duplicate_score(self, value):
        """Validate duplicate score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Duplicate score must be between 0.0 and 1.0")
        return value


class EvidenceDocumentSerializer(serializers.ModelSerializer):
    """Serializer for EvidenceDocument model"""
    
    file_name = serializers.CharField(source='file.filename', read_only=True)
    file_size = serializers.IntegerField(source='file.file_size', read_only=True)
    evidence_type_display = serializers.CharField(source='get_evidence_type_display', read_only=True)
    privilege_status_display = serializers.CharField(source='get_privilege_status_display', read_only=True)
    
    class Meta:
        model = EvidenceDocument
        fields = [
            'id', 'file', 'file_name', 'file_size', 'mass_claims_run',
            'evidence_type', 'evidence_type_display', 'is_culled', 'cull_reason',
            'relevance_score', 'contains_pii', 'privilege_status',
            'privilege_status_display', 'processing_metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_relevance_score(self, value):
        """Validate relevance score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Relevance score must be between 0.0 and 1.0")
        return value


class PIIRedactionSerializer(serializers.ModelSerializer):
    """Serializer for PIIRedaction model"""
    
    file_name = serializers.CharField(source='file.filename', read_only=True)
    pii_type_display = serializers.CharField(source='get_pii_type_display', read_only=True)
    verified_by_email = serializers.EmailField(source='verified_by.email', read_only=True)
    
    class Meta:
        model = PIIRedaction
        fields = [
            'id', 'file', 'file_name', 'mass_claims_run', 'pii_type',
            'pii_type_display', 'original_text', 'redacted_text',
            'page_number', 'position_start', 'position_end',
            'confidence_score', 'is_verified', 'verified_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_confidence_score(self, value):
        """Validate confidence score is between 0 and 1"""
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Confidence score must be between 0.0 and 1.0")
        return value
    
    def validate_page_number(self, value):
        """Validate page number is positive"""
        if value <= 0:
            raise serializers.ValidationError("Page number must be positive")
        return value


class ExhibitPackageSerializer(serializers.ModelSerializer):
    """Serializer for ExhibitPackage model"""
    
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    file_count = serializers.SerializerMethodField()
    file_names = serializers.SerializerMethodField()
    package_name = serializers.CharField(required=False, default='Untitled Package')
    bates_prefix = serializers.CharField(required=False, default='BATES')
    bates_start = serializers.CharField(required=False, default='000001')
    bates_end = serializers.CharField(required=False, default='000001')
    
    class Meta:
        model = ExhibitPackage
        fields = [
            'id', 'mass_claims_run', 'package_name', 'description',
            'files', 'file_count', 'file_names', 'bates_prefix',
            'bates_start', 'bates_end', 'total_pages', 'status',
            'status_display', 'production_metadata', 'created_at',
            'updated_at', 'produced_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def get_file_count(self, obj):
        """Get number of files in package"""
        return obj.files.count()
    
    def get_file_names(self, obj):
        """Get list of file names in package"""
        return list(obj.files.values_list('filename', flat=True))
    
    def validate_total_pages(self, value):
        """Validate total pages is non-negative"""
        if value < 0:
            raise serializers.ValidationError("Total pages must be non-negative")
        return value


class SettlementTrackingSerializer(serializers.ModelSerializer):
    """Serializer for SettlementTracking model"""
    
    settlement_type_display = serializers.CharField(source='get_settlement_type_display', read_only=True)
    notice_type_display = serializers.CharField(source='get_notice_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    case_name = serializers.CharField(source='mass_claims_run.case_name', read_only=True)
    settlement_type = serializers.CharField(required=False, default='class_wide')
    total_settlement_amount = serializers.DecimalField(max_digits=15, decimal_places=2, required=False, default=1)
    
    class Meta:
        model = SettlementTracking
        fields = [
            'id', 'mass_claims_run', 'case_name', 'settlement_type',
            'settlement_type_display', 'total_settlement_amount',
            'attorney_fees', 'administration_costs', 'net_settlement_fund',
            'notice_type', 'notice_type_display', 'notice_deadline',
            'objection_deadline', 'opt_out_deadline', 'total_eligible_claimants',
            'total_approved_claims', 'total_distributed_amount',
            'status', 'status_display', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_total_settlement_amount(self, value):
        """Validate settlement amount is positive"""
        if value is not None and value <= 0:
            raise serializers.ValidationError("Settlement amount must be positive")
        return value
    
    def validate_attorney_fees(self, value):
        """Validate attorney fees is non-negative"""
        if value is not None and value < 0:
            raise serializers.ValidationError("Attorney fees must be non-negative")
        return value


class ClaimantCommunicationSerializer(serializers.ModelSerializer):
    """Serializer for ClaimantCommunication model"""
    
    communication_type_display = serializers.CharField(source='get_communication_type_display', read_only=True)
    claimant_id = serializers.UUIDField(source='intake_form.claimant_id', read_only=True)
    
    class Meta:
        model = ClaimantCommunication
        fields = [
            'id', 'mass_claims_run', 'intake_form', 'claimant_id',
            'communication_type', 'communication_type_display',
            'subject', 'message_content', 'sent_at', 'delivered_at',
            'read_at', 'responded_at', 'response_content',
            'requires_follow_up', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'sent_at', 'created_at', 'updated_at']


class MassClaimsRunCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating a new MassClaimsRun with associated Run"""
    
    case_name = serializers.CharField(required=False, default='Untitled Case')
    case_number = serializers.CharField(required=False, default='CA-001')
    case_type = serializers.CharField(required=False, default='class_action')
    
    class Meta:
        model = MassClaimsRun
        fields = [
            'case_name', 'case_number', 'court_jurisdiction',
            'case_type', 'claim_deadline', 'settlement_amount', 'status'
        ]
    
    def create(self, validated_data):
        """Create a new Run and associated MassClaimsRun with client context"""
        user = self.context['request'].user

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

        # Create the core Run first
        run = Run.objects.create(
            user=user,
            status='Uploaded'
        )

        # Create the MassClaimsRun with client for multi-tenancy
        mass_claims_run = MassClaimsRun.objects.create(
            run=run,
            client=client,
            **validated_data
        )

        return mass_claims_run


class IntakeFormCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating intake forms with validation"""
    
    class Meta:
        model = IntakeForm
        fields = ['mass_claims_run', 'claimant_data']
    
    def validate_claimant_data(self, value):
        """Validate required fields in claimant data"""
        required_fields = ['first_name', 'last_name', 'email']
        
        for field in required_fields:
            if field not in value or not value[field]:
                raise serializers.ValidationError(f"Required field '{field}' is missing or empty")
        
        # Validate email format
        email = value.get('email', '')
        if '@' not in email or '.' not in email:
            raise serializers.ValidationError("Invalid email format")
        
        return value
    
    def create(self, validated_data):
        """Create intake form with user assignment"""
        user = self.context['request'].user
        client = get_user_client(user)
        return IntakeForm.objects.create(user=user, client=client, **validated_data)


class EvidenceSummarySerializer(serializers.Serializer):
    """Serializer for evidence document summary statistics"""
    
    evidence_type = serializers.CharField()
    evidence_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    culled_count = serializers.IntegerField()
    pii_count = serializers.IntegerField()
    privileged_count = serializers.IntegerField()
    avg_relevance_score = serializers.FloatField()


class IntakeFormSummarySerializer(serializers.Serializer):
    """Serializer for intake form summary statistics"""
    
    processing_status = serializers.CharField()
    processing_status_display = serializers.CharField()
    total_count = serializers.IntegerField()
    duplicate_count = serializers.IntegerField()
    valid_count = serializers.IntegerField()
