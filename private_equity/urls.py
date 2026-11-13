from django.urls import path
from .views import (
    DueDiligenceRunListCreateView,
    DueDiligenceRunDetailView,
    DocumentClassificationView,
    RiskClauseExtractionView,
    IssueSpottingView,
    FindingsReportView,
    SyncDataRoomView,
    RiskClauseSummaryView,
    DocumentTypeSummaryView,
    ServiceExecutionListCreateView,
    ServiceOutputListCreateView,
    # PE Value Metrics Views
    ClosingChecklistListCreateView,
    ClosingChecklistDetailView,
    PostCloseObligationListCreateView,
    DealVelocityMetricsListView,
    ClauseLibraryListCreateView,
    DealVelocityAnalyticsView,
    ChecklistProgressView,
    # Panel Management & RFP Views
    PanelFirmListCreateView,
    RFPListCreateView,
    BidAnalysisView,
    EngagementLetterListCreateView,
    # Signature Tracking Views
    SignatureTrackerListCreateView,
    # Closing Management Views
    ConditionPrecedentListCreateView,
    ClosingBinderListCreateView,
    # Compliance Tracking Views
    CovenantListCreateView,
    ConsentFilingListCreateView,
    PortfolioComplianceView,
    RiskHeatmapView,
    # Task Status View
    PETaskStatusView,
)
from .solutions_views import (
    PortfolioCompanyListCreateView, PortfolioCompanyDetailView,
    FundListCreateView, FundDetailView,
    FinancialRecordListCreateView,
    DealTargetListCreateView, DealTargetScoreView,
    InvestmentMemoListCreateView, InvestmentMemoDetailView,
    CommercialDDListCreateView,
    EarningsAnalysisListCreateView,
    DisruptionAssessmentListCreateView,
    LBOScenarioListCreateView, LBOScenarioDetailView,
    PortfolioMetricListCreateView,
    EarlyWarningAlertListView, EarlyWarningScanView,
    ValueCreationPlanListCreateView, ValueCreationPlanDetailView,
    ValueCreationInitiativeUpdateView,
    SpendClassificationListCreateView, SpendAnalysisView,
    PricingRecommendationListCreateView,
    LPReportListCreateView,
    DDQResponseListView, DDQResponseUpdateView,
    CashFlowForecastListCreateView, CashFlowForecastDetailView,
    ExitReadinessListCreateView,
    DataQualityRunListCreateView, DataQualityRunDetailView,
    DataQualityIssueUpdateView,
)

app_name = 'private_equity'

urlpatterns = [
    # Due Diligence Run Management (also aliased as deal-workspaces for frontend compatibility)
    path('due-diligence-runs/', DueDiligenceRunListCreateView.as_view(), name='dd-runs-list-create'),
    path('due-diligence-runs/<int:pk>/', DueDiligenceRunDetailView.as_view(), name='dd-run-detail'),
    # Alias endpoints for frontend compatibility (deal-workspaces = due-diligence-runs)
    path('deal-workspaces/', DueDiligenceRunListCreateView.as_view(), name='deal-workspaces-list-create'),
    path('deal-workspaces/<int:pk>/', DueDiligenceRunDetailView.as_view(), name='deal-workspace-detail'),

    # Document Classification
    path('classify-documents/', DocumentClassificationView.as_view(), name='classify-documents'),

    # Risk Clause Extraction
    path('extract-risk-clauses/', RiskClauseExtractionView.as_view(), name='extract-risk-clauses'),

    # AI Issue Spotting
    path('issue-spotting/', IssueSpottingView.as_view(), name='issue-spotting'),

    # Findings Reports
    path('findings-reports/', FindingsReportView.as_view(), name='findings-reports'),

    # Data Room Sync
    path('sync-data-room/', SyncDataRoomView.as_view(), name='sync-data-room'),

    # Analytics and Summaries
    path('risk-clause-summary/', RiskClauseSummaryView.as_view(), name='risk-clause-summary'),
    path('document-type-summary/', DocumentTypeSummaryView.as_view(), name='document-type-summary'),

    # Service Execution & Output Tracking
    path('service-executions/', ServiceExecutionListCreateView.as_view(), name='service-executions'),
    path('service-outputs/', ServiceOutputListCreateView.as_view(), name='service-outputs'),

    # ═══════════════════════════════════════════════════════════════
    # 💼 PE VALUE METRICS ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    # Closing Checklist
    path('closing-checklists/', ClosingChecklistListCreateView.as_view(), name='closing-checklists'),
    path('closing-checklists/<int:pk>/', ClosingChecklistDetailView.as_view(), name='closing-checklist-detail'),

    # Post-Close Obligations
    path('post-close-obligations/', PostCloseObligationListCreateView.as_view(), name='post-close-obligations'),
    path('import-obligations/', PostCloseObligationListCreateView.as_view(), name='import-obligations-alias'),

    # Deal Velocity Metrics
    path('deal-velocity-metrics/', DealVelocityMetricsListView.as_view(), name='deal-velocity-metrics'),
    path('deal-velocity-analytics/', DealVelocityAnalyticsView.as_view(), name='deal-velocity-analytics'),

    # Clause Library
    path('clause-library/', ClauseLibraryListCreateView.as_view(), name='clause-library'),

    # Progress Analytics
    path('checklist-progress/', ChecklistProgressView.as_view(), name='checklist-progress'),

    # ═══════════════════════════════════════════════════════════════
    # 🏢 PANEL MANAGEMENT & RFP ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    # Panel Firms
    path('panel-firms/', PanelFirmListCreateView.as_view(), name='panel-firms'),

    # RFPs
    path('rfps/', RFPListCreateView.as_view(), name='rfps'),

    # Bid Analysis
    path('bid-analysis/', BidAnalysisView.as_view(), name='bid-analysis'),

    # Engagement Letters
    path('engagement-letters/', EngagementLetterListCreateView.as_view(), name='engagement-letters'),

    # ═══════════════════════════════════════════════════════════════
    # ✍️ SIGNATURE TRACKING ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    # Signature Trackers
    path('signature-trackers/', SignatureTrackerListCreateView.as_view(), name='signature-trackers'),
    path('signature-tracker/', SignatureTrackerListCreateView.as_view(), name='signature-tracker-alias'),

    # ═══════════════════════════════════════════════════════════════
    # 📋 CLOSING MANAGEMENT ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    # Conditions Precedent
    path('conditions-precedent/', ConditionPrecedentListCreateView.as_view(), name='conditions-precedent'),
    path('cp-tracker/', ConditionPrecedentListCreateView.as_view(), name='cp-tracker-alias'),

    # Closing Binders
    path('closing-binders/', ClosingBinderListCreateView.as_view(), name='closing-binders'),
    path('closing-binder/', ClosingBinderListCreateView.as_view(), name='closing-binder-alias'),

    # ═══════════════════════════════════════════════════════════════
    # 📊 COMPLIANCE TRACKING ENDPOINTS
    # ═══════════════════════════════════════════════════════════════

    # Covenants
    path('covenants/', CovenantListCreateView.as_view(), name='covenants'),
    path('covenant-tracker/', CovenantListCreateView.as_view(), name='covenant-tracker-alias'),

    # Consent Filings
    path('consent-filings/', ConsentFilingListCreateView.as_view(), name='consent-filings'),
    path('consent-filing-tracker/', ConsentFilingListCreateView.as_view(), name='consent-filing-tracker-alias'),

    # Portfolio Compliance Dashboard
    path('portfolio-compliance/', PortfolioComplianceView.as_view(), name='portfolio-compliance'),

    # Risk Heatmap
    path('risk-heatmap/', RiskHeatmapView.as_view(), name='risk-heatmap'),

    # ═══════════════════════════════════════════════════════════════
    # 📊 TASK STATUS ENDPOINT
    # ═══════════════════════════════════════════════════════════════

    # Task Status (similar to TranslationTaskStatusView)
    path('task-status/', PETaskStatusView.as_view(), name='task-status'),

    # ═══════════════════════════════════════════════════════════════
    # 🚀 PE AI-POWERED SOLUTIONS (15 Solutions)
    # ═══════════════════════════════════════════════════════════════

    # Foundation: Portfolio Companies
    path('portfolio-companies/', PortfolioCompanyListCreateView.as_view(), name='portfolio-companies'),
    path('portfolio-companies/<int:pk>/', PortfolioCompanyDetailView.as_view(), name='portfolio-company-detail'),

    # Foundation: Funds
    path('funds/', FundListCreateView.as_view(), name='funds'),
    path('funds/<int:pk>/', FundDetailView.as_view(), name='fund-detail'),

    # Foundation: Financial Records
    path('financial-records/', FinancialRecordListCreateView.as_view(), name='financial-records'),

    # Solution 1: Deal-Sourcing Intelligence
    path('deal-targets/', DealTargetListCreateView.as_view(), name='deal-targets'),
    path('deal-targets/<int:pk>/score/', DealTargetScoreView.as_view(), name='deal-target-score'),

    # Solution 2: IC Copilot
    path('investment-memos/', InvestmentMemoListCreateView.as_view(), name='investment-memos'),
    path('investment-memos/<int:pk>/', InvestmentMemoDetailView.as_view(), name='investment-memo-detail'),

    # Solution 3: Commercial DD Platform
    path('commercial-dd/', CommercialDDListCreateView.as_view(), name='commercial-dd'),

    # Solution 4: Quality-of-Earnings
    path('earnings-analyses/', EarningsAnalysisListCreateView.as_view(), name='earnings-analyses'),

    # Solution 5: AI Disruption Scanner
    path('disruption-assessments/', DisruptionAssessmentListCreateView.as_view(), name='disruption-assessments'),

    # Solution 7: LBO & Valuation
    path('lbo-scenarios/', LBOScenarioListCreateView.as_view(), name='lbo-scenarios'),
    path('lbo-scenarios/<int:pk>/', LBOScenarioDetailView.as_view(), name='lbo-scenario-detail'),

    # Solution 8: Portfolio Early-Warning
    path('portfolio-metrics/', PortfolioMetricListCreateView.as_view(), name='portfolio-metrics'),
    path('early-warning-alerts/', EarlyWarningAlertListView.as_view(), name='early-warning-alerts'),
    path('early-warning-scan/', EarlyWarningScanView.as_view(), name='early-warning-scan'),

    # Solution 9: Value-Creation OS
    path('value-creation-plans/', ValueCreationPlanListCreateView.as_view(), name='value-creation-plans'),
    path('value-creation-plans/<int:pk>/', ValueCreationPlanDetailView.as_view(), name='value-creation-plan-detail'),
    path('value-creation-initiatives/<int:pk>/', ValueCreationInitiativeUpdateView.as_view(), name='value-creation-initiative-update'),

    # Solution 10: Cross-Portfolio Procurement
    path('spend-classifications/', SpendClassificationListCreateView.as_view(), name='spend-classifications'),
    path('spend-analysis/', SpendAnalysisView.as_view(), name='spend-analysis'),

    # Solution 11: Revenue & Pricing Optimizer
    path('pricing-recommendations/', PricingRecommendationListCreateView.as_view(), name='pricing-recommendations'),

    # Solution 12: LP Reporting & DDQ
    path('lp-reports/', LPReportListCreateView.as_view(), name='lp-reports'),
    path('ddq-responses/', DDQResponseListView.as_view(), name='ddq-responses'),
    path('ddq-responses/<int:pk>/', DDQResponseUpdateView.as_view(), name='ddq-response-update'),

    # Solution 13: Fund Cash-Flow Forecaster
    path('cashflow-forecasts/', CashFlowForecastListCreateView.as_view(), name='cashflow-forecasts'),
    path('cashflow-forecasts/<int:pk>/', CashFlowForecastDetailView.as_view(), name='cashflow-forecast-detail'),

    # Solution 14: Exit-Readiness Platform
    path('exit-readiness-assessments/', ExitReadinessListCreateView.as_view(), name='exit-readiness'),

    # Solution 15: Data Quality Platform
    path('data-quality-runs/', DataQualityRunListCreateView.as_view(), name='data-quality-runs'),
    path('data-quality-runs/<int:pk>/', DataQualityRunDetailView.as_view(), name='data-quality-run-detail'),
    path('data-quality-issues/<int:pk>/', DataQualityIssueUpdateView.as_view(), name='data-quality-issue-update'),
]
