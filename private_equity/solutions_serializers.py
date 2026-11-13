from rest_framework import serializers
from .solutions_models import (
    PortfolioCompany, Fund, FinancialRecord,
    DealTarget, InvestmentMemo, CommercialDDAssessment,
    EarningsAnalysis, DisruptionAssessment, LBOScenario,
    PortfolioMetric, EarlyWarningAlert,
    ValueCreationPlan, ValueCreationInitiative,
    SpendClassification, PricingRecommendation,
    LPReport, DDQResponse, CashFlowForecast,
    ExitReadinessAssessment, DataQualityRun, DataQualityIssue,
)


class PortfolioCompanySerializer(serializers.ModelSerializer):
    fund_names = serializers.SerializerMethodField()

    class Meta:
        model = PortfolioCompany
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_fund_names(self, obj):
        return [f.name for f in obj.funds.all()]


class FundSerializer(serializers.ModelSerializer):
    company_count = serializers.SerializerMethodField()

    class Meta:
        model = Fund
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_company_count(self, obj):
        return obj.companies.count()


class FinancialRecordSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = FinancialRecord
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class DealTargetSerializer(serializers.ModelSerializer):
    class Meta:
        model = DealTarget
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class InvestmentMemoSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = InvestmentMemo
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class CommercialDDAssessmentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = CommercialDDAssessment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class EarningsAnalysisSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = EarningsAnalysis
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class DisruptionAssessmentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = DisruptionAssessment
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class LBOScenarioSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = LBOScenario
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class PortfolioMetricSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = PortfolioMetric
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class EarlyWarningAlertSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = EarlyWarningAlert
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class ValueCreationPlanSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    initiative_count = serializers.SerializerMethodField()

    class Meta:
        model = ValueCreationPlan
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_initiative_count(self, obj):
        return obj.initiatives.count()


class ValueCreationInitiativeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValueCreationInitiative
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class SpendClassificationSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = SpendClassification
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class PricingRecommendationSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = PricingRecommendation
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class LPReportSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(source='fund.name', read_only=True)

    class Meta:
        model = LPReport
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class DDQResponseSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(source='fund.name', read_only=True)

    class Meta:
        model = DDQResponse
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class CashFlowForecastSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(source='fund.name', read_only=True)

    class Meta:
        model = CashFlowForecast
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ExitReadinessAssessmentSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = ExitReadinessAssessment
        fields = '__all__'
        read_only_fields = ['id', 'created_at']


class DataQualityRunSerializer(serializers.ModelSerializer):
    issue_count = serializers.SerializerMethodField()

    class Meta:
        model = DataQualityRun
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'completed_at']

    def get_issue_count(self, obj):
        return obj.issues.count()


class DataQualityIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataQualityIssue
        fields = '__all__'
        read_only_fields = ['id', 'created_at']
