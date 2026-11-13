from rest_framework import serializers
from django.contrib.auth import get_user_model
from core.models import File, Run
from core.utils import get_user_client
from custom_authentication.models import Client
from .models import (
    WorkplaceCommunicationsRun, CommunicationMessage, WageHourAnalysis,
    PolicyComparison, EEOCPacket, CommunicationPattern, ComplianceAlert
)

User = get_user_model()


class WorkplaceCommunicationsRunSerializer(serializers.ModelSerializer):
    """Serializer for WorkplaceCommunicationsRun model"""
    
    run_id = serializers.UUIDField(source='run.run_id', read_only=True)
    user_email = serializers.EmailField(source='run.user.email', read_only=True)
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    
    total_messages = serializers.SerializerMethodField()
    total_alerts = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkplaceCommunicationsRun
        fields = [
            'id', 'run_id', 'user_email', 'case_name', 'company_name',
            'case_type', 'case_type_display', 'data_sources', 'analysis_start_date',
            'analysis_end_date', 'key_personnel', 'total_messages', 'total_alerts',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_total_messages(self, obj):
        """Get total number of communication messages"""
        return obj.messages.count()
    
    def get_total_alerts(self, obj):
        """Get total number of compliance alerts"""
        return obj.compliance_alerts.count()


class CommunicationMessageSerializer(serializers.ModelSerializer):
    """Serializer for CommunicationMessage model"""
    
    file_name = serializers.CharField(source='file.filename', read_only=True)
    message_type_display = serializers.CharField(source='get_message_type_display', read_only=True)
    
    class Meta:
        model = CommunicationMessage
        fields = [
            'id', 'file', 'file_name', 'communications_run', 'message_id',
            'message_type', 'message_type_display', 'sender', 'recipients',
            'subject', 'content', 'sent_datetime', 'sentiment_score',
            'toxicity_score', 'relevance_score', 'is_privileged',
            'contains_pii', 'is_flagged', 'flag_reason', 'processing_metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_sentiment_score(self, value):
        """Validate sentiment score is between -1 and 1"""
        if value is not None and not -1.0 <= value <= 1.0:
            raise serializers.ValidationError("Sentiment score must be between -1.0 and 1.0")
        return value
    
    def validate_toxicity_score(self, value):
        """Validate toxicity score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Toxicity score must be between 0.0 and 1.0")
        return value
    
    def validate_relevance_score(self, value):
        """Validate relevance score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Relevance score must be between 0.0 and 1.0")
        return value


class WageHourAnalysisSerializer(serializers.ModelSerializer):
    """Serializer for WageHourAnalysis model"""
    
    class Meta:
        model = WageHourAnalysis
        fields = [
            'id', 'communications_run', 'employee_name', 'employee_id',
            'job_title', 'department', 'analysis_start_date', 'analysis_end_date',
            'total_hours_worked', 'regular_hours', 'overtime_hours',
            'early_morning_messages', 'late_evening_messages', 'weekend_messages',
            'hourly_rate', 'regular_pay', 'overtime_pay', 'total_pay',
            'potential_overtime_violations', 'potential_break_violations',
            'potential_meal_violations', 'analysis_metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_hourly_rate(self, value):
        """Validate hourly rate is positive"""
        if value is not None and value <= 0:
            raise serializers.ValidationError("Hourly rate must be positive")
        return value
    
    def validate(self, data):
        """Validate analysis date range"""
        start_date = data.get('analysis_start_date')
        end_date = data.get('analysis_end_date')
        
        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError("Analysis start date must be before end date")
        
        return data


class PolicyComparisonSerializer(serializers.ModelSerializer):
    """Serializer for PolicyComparison model"""
    
    policy_type_display = serializers.CharField(source='get_policy_type_display', read_only=True)
    policy_document_name = serializers.CharField(source='policy_document.filename', read_only=True)
    
    class Meta:
        model = PolicyComparison
        fields = [
            'id', 'communications_run', 'policy_name', 'policy_type',
            'policy_type_display', 'policy_document', 'policy_document_name',
            'policy_text', 'compliance_score', 'violations_found',
            'recommendations', 'best_practices_score', 'missing_elements',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def validate_compliance_score(self, value):
        """Validate compliance score is between 0 and 1"""
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Compliance score must be between 0.0 and 1.0")
        return value
    
    def validate_best_practices_score(self, value):
        """Validate best practices score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Best practices score must be between 0.0 and 1.0")
        return value


class EEOCPacketSerializer(serializers.ModelSerializer):
    """Serializer for EEOCPacket model"""
    
    complaint_type_display = serializers.CharField(source='get_complaint_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    message_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    packet_name = serializers.CharField(required=False, default='EEOC Packet')
    complainant_name = serializers.CharField(required=False, default='Unknown Complainant')
    complaint_type = serializers.CharField(required=False, default='discrimination')
    incident_date = serializers.DateField(required=False, default=None, allow_null=True)
    complaint_summary = serializers.CharField(required=False, default='Pending analysis', allow_blank=True)
    relevant_messages = serializers.JSONField(required=False, default=list)
    supporting_documents = serializers.JSONField(required=False, default=list)
    
    class Meta:
        model = EEOCPacket
        fields = [
            'id', 'communications_run', 'packet_name', 'complainant_name',
            'complainant_title', 'complainant_department', 'complaint_type',
            'complaint_type_display', 'incident_date', 'complaint_summary',
            'relevant_messages', 'supporting_documents', 'message_count',
            'document_count', 'evidence_strength_score', 'timeline_analysis',
            'key_findings', 'status', 'status_display', 'generation_metadata',
            'created_at', 'updated_at', 'submitted_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def get_message_count(self, obj):
        """Get number of relevant messages"""
        return obj.relevant_messages.count()
    
    def get_document_count(self, obj):
        """Get number of supporting documents"""
        return obj.supporting_documents.count()
    
    def validate_evidence_strength_score(self, value):
        """Validate evidence strength score is between 0 and 1"""
        if value is not None and not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Evidence strength score must be between 0.0 and 1.0")
        return value


class CommunicationPatternSerializer(serializers.ModelSerializer):
    """Serializer for CommunicationPattern model"""
    
    pattern_type_display = serializers.CharField(source='get_pattern_type_display', read_only=True)
    supporting_message_count = serializers.SerializerMethodField()
    
    class Meta:
        model = CommunicationPattern
        fields = [
            'id', 'communications_run', 'pattern_type', 'pattern_type_display',
            'pattern_name', 'description', 'involved_personnel',
            'confidence_score', 'severity_score', 'pattern_start_date',
            'pattern_end_date', 'supporting_messages', 'supporting_message_count',
            'pattern_details', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def get_supporting_message_count(self, obj):
        """Get number of supporting messages"""
        return obj.supporting_messages.count()
    
    def validate_confidence_score(self, value):
        """Validate confidence score is between 0 and 1"""
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Confidence score must be between 0.0 and 1.0")
        return value
    
    def validate_severity_score(self, value):
        """Validate severity score is between 0 and 1"""
        if not 0.0 <= value <= 1.0:
            raise serializers.ValidationError("Severity score must be between 0.0 and 1.0")
        return value
    
    def validate(self, data):
        """Validate pattern date range"""
        start_date = data.get('pattern_start_date')
        end_date = data.get('pattern_end_date')
        
        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError("Pattern start date must be before end date")
        
        return data


class ComplianceAlertSerializer(serializers.ModelSerializer):
    """Serializer for ComplianceAlert model"""
    
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)
    severity_display = serializers.CharField(source='get_severity_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    resolved_by_email = serializers.EmailField(source='resolved_by.email', read_only=True)
    related_message_count = serializers.SerializerMethodField()
    
    class Meta:
        model = ComplianceAlert
        fields = [
            'id', 'communications_run', 'alert_type', 'alert_type_display',
            'alert_title', 'alert_description', 'severity', 'severity_display',
            'priority', 'priority_display', 'related_messages',
            'related_message_count', 'status', 'status_display',
            'resolution_notes', 'resolved_by_email', 'resolved_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']
    
    def get_related_message_count(self, obj):
        """Get number of related messages"""
        return obj.related_messages.count()


class WorkplaceCommunicationsRunCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating a new WorkplaceCommunicationsRun with associated Run"""
    
    case_name = serializers.CharField(required=False, default='Untitled Matter')
    case_type = serializers.CharField(required=False, default='workplace_communications')
    company_name = serializers.CharField(required=False, default='Unknown Company')
    analysis_start_date = serializers.DateField(required=False, default=None, allow_null=True)
    analysis_end_date = serializers.DateField(required=False, default=None, allow_null=True)
    
    class Meta:
        model = WorkplaceCommunicationsRun
        fields = [
            'case_name', 'company_name', 'case_type', 'data_sources',
            'analysis_start_date', 'analysis_end_date', 'key_personnel'
        ]
    
    def create(self, validated_data):
        """Create a new Run and associated WorkplaceCommunicationsRun with client context"""
        user = self.context['request'].user
        client = get_user_client(user)

        # Ensure required fields have defaults
        validated_data.setdefault('company_name', 'Unspecified Company')

        # Create the core Run first
        run = Run.objects.create(
            user=user,
            status='Uploaded'
        )

        # Create the WorkplaceCommunicationsRun with client for multi-tenancy
        communications_run = WorkplaceCommunicationsRun.objects.create(
            run=run,
            client=client,
            **validated_data
        )

        return communications_run


class MessageAnalysisSummarySerializer(serializers.Serializer):
    """Serializer for message analysis summary statistics"""
    
    message_type = serializers.CharField()
    message_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    flagged_count = serializers.IntegerField()
    privileged_count = serializers.IntegerField()
    pii_count = serializers.IntegerField()
    avg_sentiment_score = serializers.FloatField()
    avg_toxicity_score = serializers.FloatField()


class ComplianceAlertSummarySerializer(serializers.Serializer):
    """Serializer for compliance alert summary statistics"""
    
    alert_type = serializers.CharField()
    alert_type_display = serializers.CharField()
    total_count = serializers.IntegerField()
    open_count = serializers.IntegerField()
    critical_count = serializers.IntegerField()
    high_count = serializers.IntegerField()


class WageHourSummarySerializer(serializers.Serializer):
    """Serializer for wage hour analysis summary"""
    
    total_employees_analyzed = serializers.IntegerField()
    total_overtime_hours = serializers.FloatField()
    potential_violations_count = serializers.IntegerField()
    total_unpaid_overtime = serializers.FloatField()
    avg_weekly_hours = serializers.FloatField()
