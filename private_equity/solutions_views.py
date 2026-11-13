"""
Views for all 15 PE AI-Powered Solutions.
Each view creates the entity and dispatches an async Celery task.
"""
import logging
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from custom_authentication.permissions import IsClientOrAdmin
from core.utils import clean_request_data_for_serializer

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
from .solutions_serializers import (
    PortfolioCompanySerializer, FundSerializer, FinancialRecordSerializer,
    DealTargetSerializer, InvestmentMemoSerializer, CommercialDDAssessmentSerializer,
    EarningsAnalysisSerializer, DisruptionAssessmentSerializer, LBOScenarioSerializer,
    PortfolioMetricSerializer, EarlyWarningAlertSerializer,
    ValueCreationPlanSerializer, ValueCreationInitiativeSerializer,
    SpendClassificationSerializer, PricingRecommendationSerializer,
    LPReportSerializer, DDQResponseSerializer, CashFlowForecastSerializer,
    ExitReadinessAssessmentSerializer, DataQualityRunSerializer, DataQualityIssueSerializer,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Foundation: Portfolio Companies
# ============================================================================

class PortfolioCompanyListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        companies = PortfolioCompany.objects.filter(client=request.user.client)
        serializer = PortfolioCompanySerializer(companies, many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = PortfolioCompanySerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PortfolioCompanyDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get_object(self, pk, client):
        return get_object_or_404(PortfolioCompany, pk=pk, client=client)

    def get(self, request, pk):
        obj = self.get_object(pk, request.user.client)
        return Response(PortfolioCompanySerializer(obj).data)

    def put(self, request, pk):
        obj = self.get_object(pk, request.user.client)
        serializer = PortfolioCompanySerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        obj = self.get_object(pk, request.user.client)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================================
# Foundation: Funds
# ============================================================================

class FundListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        funds = Fund.objects.filter(client=request.user.client)
        serializer = FundSerializer(funds, many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = FundSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class FundDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        obj = get_object_or_404(Fund, pk=pk, client=request.user.client)
        return Response(FundSerializer(obj).data)

    def put(self, request, pk):
        obj = get_object_or_404(Fund, pk=pk, client=request.user.client)
        serializer = FundSerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Foundation: Financial Records
# ============================================================================

class FinancialRecordListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = FinancialRecord.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = FinancialRecordSerializer(qs[:200], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = FinancialRecordSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 1: Deal-Sourcing Intelligence
# ============================================================================

class DealTargetListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = DealTarget.objects.filter(client=request.user.client)
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        serializer = DealTargetSerializer(qs[:200], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = DealTargetSerializer(data=data)
        if serializer.is_valid():
            target = serializer.save()
            from .solutions_tasks import score_deal_target_task
            llm_config = request.data.get('llm_config')
            score_deal_target_task.delay(target.id, request.data.get('thesis', ''), request.user.id, llm_config=llm_config)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DealTargetScoreView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request, pk):
        target = get_object_or_404(DealTarget, pk=pk, client=request.user.client)
        thesis = request.data.get('thesis', '')
        from .solutions_tasks import score_deal_target_task
        task = score_deal_target_task.delay(target.id, thesis, request.user.id, llm_config=request.data.get('llm_config'))
        return Response({'task_id': task.id, 'status': 'scoring', 'target_id': target.id})


# ============================================================================
# Solution 2: IC Copilot
# ============================================================================

class InvestmentMemoListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = InvestmentMemo.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = InvestmentMemoSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        data['generated_by'] = request.user.id
        serializer = InvestmentMemoSerializer(data=data)
        if serializer.is_valid():
            memo = serializer.save()
            from .solutions_tasks import generate_investment_memo_task
            generate_investment_memo_task.delay(memo.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class InvestmentMemoDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        obj = get_object_or_404(InvestmentMemo, pk=pk, client=request.user.client)
        return Response(InvestmentMemoSerializer(obj).data)


# ============================================================================
# Solution 3: Commercial DD Platform
# ============================================================================

class CommercialDDListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = CommercialDDAssessment.objects.filter(client=request.user.client)
        serializer = CommercialDDAssessmentSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = CommercialDDAssessmentSerializer(data=data)
        if serializer.is_valid():
            assessment = serializer.save()
            from .solutions_tasks import run_commercial_dd_task
            run_commercial_dd_task.delay(assessment.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 4: Quality-of-Earnings
# ============================================================================

class EarningsAnalysisListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = EarningsAnalysis.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = EarningsAnalysisSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = EarningsAnalysisSerializer(data=data)
        if serializer.is_valid():
            analysis = serializer.save()
            from .solutions_tasks import run_earnings_analysis_task
            run_earnings_analysis_task.delay(analysis.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 5: AI Disruption Scanner
# ============================================================================

class DisruptionAssessmentListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = DisruptionAssessment.objects.filter(client=request.user.client)
        serializer = DisruptionAssessmentSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = DisruptionAssessmentSerializer(data=data)
        if serializer.is_valid():
            assessment = serializer.save()
            from .solutions_tasks import run_disruption_assessment_task
            run_disruption_assessment_task.delay(assessment.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 7: LBO & Valuation Scenario
# ============================================================================

class LBOScenarioListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = LBOScenario.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = LBOScenarioSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = LBOScenarioSerializer(data=data)
        if serializer.is_valid():
            scenario = serializer.save()
            from .solutions_tasks import run_lbo_scenario_task
            run_lbo_scenario_task.delay(scenario.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LBOScenarioDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        obj = get_object_or_404(LBOScenario, pk=pk, client=request.user.client)
        return Response(LBOScenarioSerializer(obj).data)


# ============================================================================
# Solution 8: Portfolio Early-Warning System
# ============================================================================

class PortfolioMetricListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = PortfolioMetric.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = PortfolioMetricSerializer(qs[:500], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = PortfolioMetricSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EarlyWarningAlertListView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = EarlyWarningAlert.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        severity = request.query_params.get('severity')
        if severity:
            qs = qs.filter(severity=severity)
        serializer = EarlyWarningAlertSerializer(qs[:200], many=True)
        return Response(serializer.data)


class EarlyWarningScanView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        company_id = request.data.get('company_id') or request.data.get('company')
        if not company_id:
            return Response({'error': 'company_id required'}, status=status.HTTP_400_BAD_REQUEST)
        company = get_object_or_404(PortfolioCompany, pk=company_id, client=request.user.client)
        from .solutions_tasks import run_early_warning_scan_task
        task = run_early_warning_scan_task.delay(company.id, request.user.id, llm_config=request.data.get('llm_config'))
        return Response({'task_id': task.id, 'status': 'scanning', 'company_id': company.id})


# ============================================================================
# Solution 9: Value-Creation OS
# ============================================================================

class ValueCreationPlanListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = ValueCreationPlan.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = ValueCreationPlanSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = ValueCreationPlanSerializer(data=data)
        if serializer.is_valid():
            plan = serializer.save()
            from .solutions_tasks import generate_value_creation_plan_task
            generate_value_creation_plan_task.delay(plan.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ValueCreationPlanDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        plan = get_object_or_404(ValueCreationPlan, pk=pk, client=request.user.client)
        plan_data = ValueCreationPlanSerializer(plan).data
        plan_data['initiatives'] = ValueCreationInitiativeSerializer(
            plan.initiatives.all(), many=True
        ).data
        return Response(plan_data)


class ValueCreationInitiativeUpdateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def patch(self, request, pk):
        obj = get_object_or_404(ValueCreationInitiative, pk=pk, client=request.user.client)
        serializer = ValueCreationInitiativeSerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 10: Cross-Portfolio Procurement
# ============================================================================

class SpendClassificationListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = SpendClassification.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = SpendClassificationSerializer(qs[:500], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = SpendClassificationSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SpendAnalysisView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def post(self, request):
        from .solutions_tasks import analyze_spend_task
        task = analyze_spend_task.delay(request.user.client.id, request.user.id, llm_config=request.data.get('llm_config'))
        return Response({'task_id': task.id, 'status': 'analyzing'})


# ============================================================================
# Solution 11: Revenue & Pricing Optimizer
# ============================================================================

class PricingRecommendationListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = PricingRecommendation.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = PricingRecommendationSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = PricingRecommendationSerializer(data=data)
        if serializer.is_valid():
            rec = serializer.save()
            from .solutions_tasks import run_pricing_optimization_task
            run_pricing_optimization_task.delay(rec.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 12: LP Reporting & DDQ Copilot
# ============================================================================

class LPReportListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = LPReport.objects.filter(client=request.user.client)
        fund_id = request.query_params.get('fund_id')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        serializer = LPReportSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        data['generated_by'] = request.user.id
        serializer = LPReportSerializer(data=data)
        if serializer.is_valid():
            report = serializer.save()
            from .solutions_tasks import generate_lp_report_task
            generate_lp_report_task.delay(report.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DDQResponseListView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = DDQResponse.objects.filter(client=request.user.client)
        fund_id = request.query_params.get('fund_id')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        serializer = DDQResponseSerializer(qs[:500], many=True)
        return Response(serializer.data)


class DDQResponseUpdateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def patch(self, request, pk):
        obj = get_object_or_404(DDQResponse, pk=pk, client=request.user.client)
        serializer = DDQResponseSerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 13: Fund Cash-Flow Forecaster
# ============================================================================

class CashFlowForecastListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = CashFlowForecast.objects.filter(client=request.user.client)
        fund_id = request.query_params.get('fund_id')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        serializer = CashFlowForecastSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = CashFlowForecastSerializer(data=data)
        if serializer.is_valid():
            forecast = serializer.save()
            from .solutions_tasks import generate_cashflow_forecast_task
            generate_cashflow_forecast_task.delay(forecast.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CashFlowForecastDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        obj = get_object_or_404(CashFlowForecast, pk=pk, client=request.user.client)
        return Response(CashFlowForecastSerializer(obj).data)


# ============================================================================
# Solution 14: Exit-Readiness Platform
# ============================================================================

class ExitReadinessListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = ExitReadinessAssessment.objects.filter(client=request.user.client)
        company_id = request.query_params.get('company_id')
        if company_id:
            qs = qs.filter(company_id=company_id)
        serializer = ExitReadinessAssessmentSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = ExitReadinessAssessmentSerializer(data=data)
        if serializer.is_valid():
            assessment = serializer.save()
            from .solutions_tasks import run_exit_readiness_task
            run_exit_readiness_task.delay(assessment.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================================================
# Solution 15: Data Quality Platform
# ============================================================================

class DataQualityRunListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request):
        qs = DataQualityRun.objects.filter(client=request.user.client)
        serializer = DataQualityRunSerializer(qs[:100], many=True)
        return Response(serializer.data)

    def post(self, request):
        data = clean_request_data_for_serializer(request.data)
        data['client'] = request.user.client.id
        serializer = DataQualityRunSerializer(data=data)
        if serializer.is_valid():
            dq_run = serializer.save()
            from .solutions_tasks import run_data_quality_check_task
            run_data_quality_check_task.delay(dq_run.id, request.user.id, llm_config=request.data.get('llm_config'))
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DataQualityRunDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def get(self, request, pk):
        dq_run = get_object_or_404(DataQualityRun, pk=pk, client=request.user.client)
        run_data = DataQualityRunSerializer(dq_run).data
        run_data['issues'] = DataQualityIssueSerializer(
            dq_run.issues.all(), many=True
        ).data
        return Response(run_data)


class DataQualityIssueUpdateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdmin]

    def patch(self, request, pk):
        obj = get_object_or_404(DataQualityIssue, pk=pk, client=request.user.client)
        serializer = DataQualityIssueSerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
