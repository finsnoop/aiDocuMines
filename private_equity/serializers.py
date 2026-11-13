from rest_framework import serializers
from django.contrib.auth import get_user_model
from core.models import File, Run
from custom_authentication.models import Client
from .models import (
    DueDiligenceRun, DocumentClassification, RiskClause,
    FindingsReport, DataRoomConnector, ClosingChecklist,
    PostCloseObligation, DealVelocityMetrics, ClauseLibrary,
    # New PE models
    PanelFirm, RFP, RFPBid, EngagementLetter,
    SignatureTracker, ConditionPrecedent, ClosingBinder,
    Covenant, ConsentFiling
)

User = get_user_model()


class DueDiligenceRunSerializer(serializers.ModelSerializer):
    """Serializer for DueDiligenceRun model"""
    
    run_id = serializers.UUIDField(source='run.run_id', read_only=True)
    user_email = serializers.EmailField(source='run.user.email', read_only=True)
    total_documents = serializers.SerializerMethodField()
    total_risk_clauses = serializers.SerializerMethodField()
    
    class Meta:
        model = DueDiligenceRun
        fields = [
            'id', 'run_id', 'user_email', 'deal_name', 'target_company',
            'deal_type', 'data_room_source', 'deal_value', 'expected_close_date',
            'total_documents', 'total_risk_clauses', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_total_documents(self, obj):
        """Get total number of documents in this DD run"""
        return obj.document_classifications.count()
    
    def get_total_risk_clauses(self, obj):
        """Get total number of risk clauses found"""
        return obj.risk_clauses.count()
    
    def validate_deal_value(self, value):
        """Validate deal value is positive"""
        if value is not None and value <= 0:
            raise serializers.ValidationError("Deal value must be positive")
        return value


class DocumentClassificationSerializer(serializers.ModelSerializer):
    """Serializer for DocumentClassification model"""
    
    file_name = serializers.CharField(source='file.filename', read_only=True)
    file_size = serializers.IntegerField(source='file.file_size', read_only=True)
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    verified_by_email = serializers.EmailField(source='verified_by.email', read_only=True)
    
    class Meta:
        model = DocumentClassification
        fields = [
            'id', 'file', 'file_name', 'file_size', 'due_diligence_run',
            'document_type', 'document_type_display', 'confidence_score',
            'classification_metadata', 'is_verified', 'verified_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_confidence_score(self, value):
        """Validate confidence score is between 0 and 1"""
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Confidence score must be between 0.0 and 1.0")
        return value


class RiskClauseSerializer(serializers.ModelSerializer):
    """Serializer for RiskClause model"""
    
    file_name = serializers.CharField(source='file.filename', read_only=True)
    clause_type_display = serializers.CharField(source='get_clause_type_display', read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    reviewed_by_email = serializers.EmailField(source='reviewed_by.email', read_only=True)
    
    class Meta:
        model = RiskClause
        fields = [
            'id', 'file', 'file_name', 'due_diligence_run', 'clause_type',
            'clause_type_display', 'clause_text', 'risk_level', 'risk_level_display',
            'page_number', 'position_start', 'position_end', 'risk_explanation',
            'mitigation_suggestions', 'is_reviewed', 'reviewed_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_page_number(self, value):
        """Validate page number is positive"""
        if value <= 0:
            raise serializers.ValidationError("Page number must be positive")
        return value
    
    def validate_position_start(self, value):
        """Validate position_start is non-negative"""
        if value is not None and value < 0:
            raise serializers.ValidationError("Position start must be non-negative")
        return value
    
    def validate(self, data):
        """Validate position_end is greater than position_start"""
        position_start = data.get('position_start')
        position_end = data.get('position_end')
        
        if position_start is not None and position_end is not None:
            if position_end <= position_start:
                raise serializers.ValidationError(
                    "Position end must be greater than position start"
                )
        return data


class FindingsReportSerializer(serializers.ModelSerializer):
    """Serializer for FindingsReport model"""
    
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)
    target_company = serializers.CharField(source='due_diligence_run.target_company', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    finalized_by_email = serializers.EmailField(source='finalized_by.email', read_only=True)
    
    class Meta:
        model = FindingsReport
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'target_company',
            'report_name', 'executive_summary', 'document_summary',
            'risk_summary', 'key_findings', 'recommendations',
            'total_documents_reviewed', 'total_risk_clauses_found',
            'high_risk_items_count', 'status', 'status_display',
            'generated_at', 'updated_at', 'finalized_at', 'finalized_by_email'
        ]
        read_only_fields = ['id', 'user', 'generated_at', 'updated_at']
    
    def validate_total_documents_reviewed(self, value):
        """Validate total documents reviewed is non-negative"""
        if value < 0:
            raise serializers.ValidationError("Total documents reviewed must be non-negative")
        return value
    
    def validate_total_risk_clauses_found(self, value):
        """Validate total risk clauses found is non-negative"""
        if value < 0:
            raise serializers.ValidationError("Total risk clauses found must be non-negative")
        return value


class DataRoomConnectorSerializer(serializers.ModelSerializer):
    """Serializer for DataRoomConnector model"""
    
    connector_type_display = serializers.CharField(source='get_connector_type_display', read_only=True)
    sync_status_display = serializers.CharField(source='get_sync_status_display', read_only=True)
    
    class Meta:
        model = DataRoomConnector
        fields = [
            'id', 'due_diligence_run', 'connector_type', 'connector_type_display',
            'connector_name', 'connection_config', 'last_sync_at', 'sync_status',
            'sync_status_display', 'sync_error_message', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'last_sync_at', 'created_at', 'updated_at']
        extra_kwargs = {
            'connection_config': {'write_only': True}  # Don't expose sensitive config in responses
        }
    
    def validate_connector_name(self, value):
        """Validate connector name is not empty"""
        if not value.strip():
            raise serializers.ValidationError("Connector name cannot be empty")
        return value.strip()


class DueDiligenceRunCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating a new DueDiligenceRun with associated Run"""
    
    class Meta:
        model = DueDiligenceRun
        fields = [
            'deal_name', 'target_company', 'deal_type', 'data_room_source',
            'deal_value', 'expected_close_date'
        ]
    
    def create(self, validated_data):
        """Create a new Run and associated DueDiligenceRun with client context"""
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

        # Create the DueDiligenceRun with client for multi-tenancy
        due_diligence_run = DueDiligenceRun.objects.create(
            run=run,
            client=client,
            **validated_data
        )

        return due_diligence_run


class RiskClauseSummarySerializer(serializers.Serializer):
    """Serializer for risk clause summary statistics"""
    
    clause_type = serializers.CharField()
    clause_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    high_risk_count = serializers.IntegerField()
    medium_risk_count = serializers.IntegerField()
    low_risk_count = serializers.IntegerField()
    critical_risk_count = serializers.IntegerField()


class DocumentTypeSummarySerializer(serializers.Serializer):
    """Serializer for document type summary statistics"""

    document_type = serializers.CharField()
    document_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    verified_count = serializers.IntegerField()
    avg_confidence_score = serializers.FloatField()


# ═══════════════════════════════════════════════════════════════
# 💼 PE VALUE METRICS SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class ClosingChecklistSerializer(serializers.ModelSerializer):
    """Serializer for ClosingChecklist model"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    assigned_to_email = serializers.EmailField(source='assigned_to.email', read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)

    class Meta:
        model = ClosingChecklist
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'item_name', 'category', 'category_display',
            'priority', 'priority_display', 'status', 'status_display', 'assigned_to',
            'assigned_to_email', 'due_date', 'completed_date', 'depends_on',
            'blocker_notes', 'related_document', 'requires_signature',
            'signature_obtained', 'signatory_name', 'notes', 'order',
            'is_overdue', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClosingChecklistCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating ClosingChecklist items"""

    class Meta:
        model = ClosingChecklist
        fields = [
            'due_diligence_run', 'item_name', 'category', 'priority', 'status',
            'assigned_to', 'due_date', 'depends_on', 'blocker_notes',
            'related_document', 'requires_signature', 'signatory_name', 'notes', 'order'
        ]

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client

        return ClosingChecklist.objects.create(
            client=client,
            created_by=user,
            **validated_data
        )


class PostCloseObligationSerializer(serializers.ModelSerializer):
    """Serializer for PostCloseObligation model"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    obligation_type_display = serializers.CharField(source='get_obligation_type_display', read_only=True)
    frequency_display = serializers.CharField(source='get_frequency_display', read_only=True)
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    assigned_to_email = serializers.EmailField(source='assigned_to.email', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)

    class Meta:
        model = PostCloseObligation
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'obligation_name', 'obligation_type',
            'obligation_type_display', 'description', 'source_document', 'source_clause',
            'status', 'status_display', 'frequency', 'frequency_display',
            'effective_date', 'due_date', 'completion_date', 'next_due_date',
            'responsible_party', 'assigned_to', 'assigned_to_email',
            'risk_level', 'risk_level_display', 'non_compliance_impact',
            'notes', 'evidence_file', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class PostCloseObligationCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating PostCloseObligation items"""

    class Meta:
        model = PostCloseObligation
        fields = [
            'due_diligence_run', 'obligation_name', 'obligation_type', 'description',
            'source_document', 'source_clause', 'status', 'frequency',
            'effective_date', 'due_date', 'next_due_date', 'responsible_party',
            'assigned_to', 'risk_level', 'non_compliance_impact', 'notes'
        ]

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client

        return PostCloseObligation.objects.create(
            client=client,
            created_by=user,
            **validated_data
        )


class DealVelocityMetricsSerializer(serializers.ModelSerializer):
    """Serializer for DealVelocityMetrics model"""

    phase_display = serializers.CharField(source='get_phase_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)
    variance_days = serializers.IntegerField(read_only=True)
    is_delayed = serializers.BooleanField(read_only=True)

    class Meta:
        model = DealVelocityMetrics
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'phase', 'phase_display',
            'phase_start_date', 'phase_end_date', 'planned_duration_days',
            'actual_duration_days', 'variance_days', 'is_delayed',
            'is_bottleneck', 'bottleneck_reason', 'bottleneck_resolved',
            'bottleneck_resolution', 'team_members_involved', 'external_parties_involved',
            'issues_identified', 'issues_resolved', 'documents_reviewed',
            'notes', 'key_milestones', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ClauseLibrarySerializer(serializers.ModelSerializer):
    """Serializer for ClauseLibrary model"""

    clause_category_display = serializers.CharField(source='get_clause_category_display', read_only=True)
    risk_position_display = serializers.CharField(source='get_risk_position_display', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = ClauseLibrary
        fields = [
            'id', 'clause_name', 'clause_category', 'clause_category_display',
            'clause_text', 'risk_position', 'risk_position_display', 'deal_types',
            'usage_count', 'last_used_date', 'version', 'is_active', 'parent_clause',
            'usage_notes', 'negotiation_tips', 'tags', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'usage_count', 'last_used_date', 'created_at', 'updated_at']


class ClauseLibraryCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating ClauseLibrary items"""

    class Meta:
        model = ClauseLibrary
        fields = [
            'clause_name', 'clause_category', 'clause_text', 'risk_position',
            'deal_types', 'usage_notes', 'negotiation_tips', 'tags'
        ]

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client

        return ClauseLibrary.objects.create(
            client=client,
            created_by=user,
            **validated_data
        )


# ═══════════════════════════════════════════════════════════════
# 📊 PE VALUE ANALYTICS SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class DealVelocitySummarySerializer(serializers.Serializer):
    """Serializer for deal velocity summary analytics"""

    total_deals = serializers.IntegerField()
    avg_deal_duration_days = serializers.FloatField()
    deals_on_track = serializers.IntegerField()
    deals_delayed = serializers.IntegerField()
    total_bottlenecks = serializers.IntegerField()
    resolved_bottlenecks = serializers.IntegerField()
    avg_phase_duration = serializers.DictField()  # Phase name -> avg days


class ChecklistProgressSerializer(serializers.Serializer):
    """Serializer for closing checklist progress analytics"""

    deal_name = serializers.CharField()
    total_items = serializers.IntegerField()
    completed_items = serializers.IntegerField()
    in_progress_items = serializers.IntegerField()
    blocked_items = serializers.IntegerField()
    overdue_items = serializers.IntegerField()
    completion_percentage = serializers.FloatField()
    items_by_category = serializers.DictField()
    items_by_priority = serializers.DictField()


class PostCloseObligationSummarySerializer(serializers.Serializer):
    """Serializer for post-close obligation summary analytics"""

    total_obligations = serializers.IntegerField()
    pending_obligations = serializers.IntegerField()
    completed_obligations = serializers.IntegerField()
    overdue_obligations = serializers.IntegerField()
    upcoming_due_dates = serializers.ListField(child=serializers.DictField())
    obligations_by_type = serializers.DictField()
    high_risk_obligations = serializers.IntegerField()


# ═══════════════════════════════════════════════════════════════
# 🏢 PANEL MANAGEMENT & RFP SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class PanelFirmSerializer(serializers.ModelSerializer):
    """Serializer for PanelFirm model"""

    class Meta:
        model = PanelFirm
        fields = [
            'id', 'name', 'practice_areas', 'regions',
            'primary_contact_name', 'primary_contact_email', 'primary_contact_phone',
            'standard_hourly_rate', 'discounted_rate', 'billing_arrangements',
            'total_deals', 'avg_deal_value', 'avg_rating', 'on_time_delivery_rate',
            'is_active', 'is_preferred', 'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client
        return PanelFirm.objects.create(client=client, **validated_data)


class RFPSerializer(serializers.ModelSerializer):
    """Serializer for RFP model"""

    matter_type_display = serializers.CharField(source='get_matter_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True, allow_null=True)
    winning_firm_name = serializers.CharField(source='winning_firm.name', read_only=True, allow_null=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    bids_count = serializers.SerializerMethodField()

    class Meta:
        model = RFP
        fields = [
            'id', 'title', 'matter_type', 'matter_type_display', 'description',
            'due_diligence_run', 'deal_name', 'scope_of_work', 'estimated_value',
            'requirements', 'response_deadline', 'project_start_date', 'project_end_date',
            'status', 'status_display', 'invited_firms', 'winning_firm', 'winning_firm_name',
            'winning_bid_amount', 'bids_count', 'created_by_email', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_bids_count(self, obj):
        return obj.bids.count() if hasattr(obj, 'bids') else 0

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client
        invited_firms = validated_data.pop('invited_firms', [])
        rfp = RFP.objects.create(client=client, created_by=user, **validated_data)
        if invited_firms:
            rfp.invited_firms.set(invited_firms)
        return rfp


class RFPBidSerializer(serializers.ModelSerializer):
    """Serializer for RFPBid model"""

    firm_name = serializers.CharField(source='firm.name', read_only=True)
    rfp_title = serializers.CharField(source='rfp.title', read_only=True)
    fee_structure_display = serializers.CharField(source='get_fee_structure_display', read_only=True)

    class Meta:
        model = RFPBid
        fields = [
            'id', 'rfp', 'rfp_title', 'firm', 'firm_name',
            'proposed_fee', 'fee_structure', 'fee_structure_display',
            'proposed_timeline', 'team_composition',
            'price_score', 'experience_score', 'team_score', 'timeline_score', 'overall_score',
            'is_selected', 'rejection_reason', 'proposal_file',
            'submitted_at', 'updated_at'
        ]
        read_only_fields = ['id', 'submitted_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        client = request.user.client
        return RFPBid.objects.create(client=client, **validated_data)


class EngagementLetterSerializer(serializers.ModelSerializer):
    """Serializer for EngagementLetter model"""

    firm_name = serializers.CharField(source='firm.name', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True, allow_null=True)
    fee_arrangement_display = serializers.CharField(source='get_fee_arrangement_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = EngagementLetter
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'firm', 'firm_name', 'rfp',
            'scope_description', 'fee_arrangement', 'fee_arrangement_display',
            'agreed_fee', 'fee_cap', 'engagement_start_date', 'estimated_completion_date',
            'billing_frequency', 'payment_terms', 'status', 'status_display',
            'generated_document', 'signed_document', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client
        return EngagementLetter.objects.create(client=client, created_by=user, **validated_data)


# ═══════════════════════════════════════════════════════════════
# 📋 CLOSING AUTOMATION SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class SignatureTrackerSerializer(serializers.ModelSerializer):
    """Serializer for SignatureTracker model"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    esign_provider_display = serializers.CharField(source='get_esign_provider_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)

    class Meta:
        model = SignatureTracker
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'document_name', 'document_file',
            'signatory_name', 'signatory_email', 'signatory_role',
            'status', 'status_display', 'esign_provider', 'esign_provider_display',
            'esign_envelope_id', 'sent_at', 'viewed_at', 'signed_at', 'due_date',
            'signed_document', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        client = request.user.client
        return SignatureTracker.objects.create(client=client, **validated_data)


class ConditionPrecedentSerializer(serializers.ModelSerializer):
    """Serializer for ConditionPrecedent model"""

    status_display = serializers.CharField(source='get_status_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)
    assigned_to_email = serializers.EmailField(source='assigned_to.email', read_only=True, allow_null=True)

    class Meta:
        model = ConditionPrecedent
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'name', 'description',
            'category', 'category_display', 'status', 'status_display',
            'responsible_party', 'assigned_to', 'assigned_to_email',
            'target_date', 'satisfied_date', 'blocker_description',
            'blocker_resolution_plan', 'evidence_file', 'order',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        client = request.user.client
        return ConditionPrecedent.objects.create(client=client, **validated_data)


class ClosingBinderSerializer(serializers.ModelSerializer):
    """Serializer for ClosingBinder model"""

    format_display = serializers.CharField(source='get_format_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    documents_count = serializers.SerializerMethodField()

    class Meta:
        model = ClosingBinder
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'name', 'format', 'format_display',
            'include_toc', 'include_signature_pages', 'status', 'status_display',
            'pdf_file', 'zip_file', 'table_of_contents', 'documents_count',
            'error_message', 'created_by_email', 'created_at', 'completed_at'
        ]
        read_only_fields = ['id', 'created_at', 'completed_at']

    def get_documents_count(self, obj):
        return obj.included_documents.count() if hasattr(obj, 'included_documents') else 0

    def create(self, validated_data):
        request = self.context.get('request')
        user = request.user
        client = user.client
        return ClosingBinder.objects.create(client=client, created_by=user, **validated_data)


# ═══════════════════════════════════════════════════════════════
# 📊 POST-CLOSE TRACKING SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class CovenantSerializer(serializers.ModelSerializer):
    """Serializer for Covenant model"""

    covenant_type_display = serializers.CharField(source='get_covenant_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    threshold_type_display = serializers.CharField(source='get_threshold_type_display', read_only=True)
    reporting_frequency_display = serializers.CharField(source='get_reporting_frequency_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)

    class Meta:
        model = Covenant
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'name', 'covenant_type',
            'covenant_type_display', 'description', 'metric_name', 'threshold_value',
            'threshold_type', 'threshold_type_display', 'current_value',
            'status', 'status_display', 'reporting_frequency', 'reporting_frequency_display',
            'next_reporting_date', 'last_reported_date', 'source_document', 'source_clause',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        client = request.user.client
        return Covenant.objects.create(client=client, **validated_data)


class ConsentFilingSerializer(serializers.ModelSerializer):
    """Serializer for ConsentFiling model"""

    filing_type_display = serializers.CharField(source='get_filing_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    deal_name = serializers.CharField(source='due_diligence_run.deal_name', read_only=True)
    assigned_to_email = serializers.EmailField(source='assigned_to.email', read_only=True, allow_null=True)

    class Meta:
        model = ConsentFiling
        fields = [
            'id', 'due_diligence_run', 'deal_name', 'name', 'filing_type',
            'filing_type_display', 'description', 'filing_party', 'receiving_party',
            'status', 'status_display', 'assigned_to', 'assigned_to_email',
            'due_date', 'submitted_date', 'approved_date',
            'filing_document', 'approval_document', 'notes', 'rejection_reason',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        client = request.user.client
        return ConsentFiling.objects.create(client=client, **validated_data)


# ═══════════════════════════════════════════════════════════════
# 📊 ADDITIONAL ANALYTICS SERIALIZERS
# ═══════════════════════════════════════════════════════════════

class PortfolioComplianceSerializer(serializers.Serializer):
    """Serializer for the portfolio compliance dashboard (covenants and consent filings)."""

    total_covenants = serializers.IntegerField()
    compliant_covenants = serializers.IntegerField()
    breached_covenants = serializers.IntegerField()
    at_risk_covenants = serializers.IntegerField()
    covenant_compliance_rate = serializers.FloatField()
    total_filings = serializers.IntegerField()
    completed_filings = serializers.IntegerField()
    pending_filings = serializers.IntegerField()
    filing_completion_rate = serializers.FloatField()
    overdue_covenants = serializers.IntegerField()
    overdue_filings = serializers.IntegerField()


class RiskHeatmapSerializer(serializers.Serializer):
    """Serializer for the deal risk heatmap."""

    deals = serializers.ListField(child=serializers.DictField())
    total_deals = serializers.IntegerField()
    high_risk_deals = serializers.IntegerField()


class BidAnalysisSerializer(serializers.Serializer):
    """Serializer for bid analysis comparison"""

    rfp_id = serializers.IntegerField()
    rfp_title = serializers.CharField()
    bids = serializers.ListField(child=serializers.DictField())
    comparison_matrix = serializers.ListField(child=serializers.DictField())
    recommendation = serializers.DictField()
