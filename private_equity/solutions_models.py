"""
Models for the 15 PE AI-Powered Solutions.
All models follow multi-tenancy via client FK.
"""
from django.db import models
from django.conf import settings
import uuid

User = settings.AUTH_USER_MODEL


class PortfolioCompany(models.Model):
    """Central entity linking all PE data across solutions."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_portfolio_companies')
    name = models.CharField(max_length=255)
    sector = models.CharField(max_length=255, blank=True, default='')
    sub_sector = models.CharField(max_length=255, blank=True, default='')
    headquarters = models.CharField(max_length=255, blank=True, default='')
    website = models.URLField(blank=True, default='')
    employee_count = models.IntegerField(null=True, blank=True)
    founded_year = models.IntegerField(null=True, blank=True)
    description = models.TextField(blank=True, default='')
    investment_thesis = models.TextField(blank=True, default='')
    deal_workspace = models.ForeignKey('private_equity.DueDiligenceRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='portfolio_companies')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_portfolio_company'
        ordering = ['name']

    def __str__(self):
        return self.name


class Fund(models.Model):
    """Fund-level entity for LP reporting and cash-flow forecasting."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_funds')
    name = models.CharField(max_length=255)
    vintage_year = models.IntegerField()
    fund_size = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    strategy = models.CharField(max_length=100, blank=True, default='')
    geographic_focus = models.CharField(max_length=255, blank=True, default='')
    sector_focus = models.CharField(max_length=255, blank=True, default='')
    fund_manager = models.CharField(max_length=255, blank=True, default='')
    target_irr = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    target_tvpi = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    investment_period_end = models.DateField(null=True, blank=True)
    fund_life_end = models.DateField(null=True, blank=True)
    lp_count = models.IntegerField(default=0)
    capital_called = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    capital_distributed = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    companies = models.ManyToManyField(PortfolioCompany, blank=True, related_name='funds')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_fund'
        ordering = ['-vintage_year']

    def __str__(self):
        return f"{self.name} ({self.vintage_year})"


class FinancialRecord(models.Model):
    """Normalized financial data per company per period."""
    PERIOD_TYPES = [('monthly', 'Monthly'), ('quarterly', 'Quarterly'), ('annual', 'Annual')]
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_financial_records')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='financial_records')
    period_type = models.CharField(max_length=20, choices=PERIOD_TYPES, default='quarterly')
    period_end = models.DateField()
    revenue = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cogs = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    gross_profit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    operating_expenses = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    ebitda = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    net_income = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_assets = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_debt = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    cash = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    working_capital = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    capex = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    fcf = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    headcount = models.IntegerField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    source_files = models.ManyToManyField('core.File', blank=True, related_name='pe_financial_records')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_financial_record'
        ordering = ['-period_end']
        unique_together = ['company', 'period_end', 'period_type']

    def __str__(self):
        return f"{self.company.name} - {self.period_end}"


# ============================================================================
# Solution 1: Deal-Sourcing Intelligence Engine
# ============================================================================

class DealTarget(models.Model):
    """Companies identified by the deal-sourcing engine."""
    SOURCE_TYPES = [
        ('proprietary', 'Proprietary'), ('brokered', 'Brokered'),
        ('bankruptcy', 'Bankruptcy'), ('succession', 'Succession'),
        ('carve_out', 'Carve-Out'), ('consolidation', 'Consolidation'),
    ]
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_deal_targets')
    fund = models.ForeignKey(Fund, on_delete=models.SET_NULL, null=True, blank=True, related_name='targets')
    name = models.CharField(max_length=255, default='Untitled Target')
    sector = models.CharField(max_length=255, blank=True, default='')
    sub_sector = models.CharField(max_length=255, blank=True, default='')
    source_type = models.CharField(max_length=50, choices=SOURCE_TYPES, default='proprietary')
    source_url = models.URLField(blank=True, default='')
    headquarters = models.CharField(max_length=255, blank=True, default='')
    employee_count = models.IntegerField(null=True, blank=True)
    estimated_revenue = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    thesis_alignment_score = models.IntegerField(default=0, help_text="0-100")
    ai_rationale = models.TextField(blank=True, default='')
    thesis_alignment_points = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    recommended_channel = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=30, default='identified', choices=[
        ('identified', 'Identified'), ('screened', 'Screened'), ('outreach', 'Outreach'),
        ('pitching', 'Pitching'), ('diligence', 'In Diligence'), ('passed', 'Passed'),
    ])
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_deal_target'
        ordering = ['-thesis_alignment_score']

    def __str__(self):
        return f"{self.name} ({self.thesis_alignment_score})"


# ============================================================================
# Solution 2: Investment Committee Copilot
# ============================================================================

class InvestmentMemo(models.Model):
    """AI-generated investment committee memo."""
    STATUS_CHOICES = [('draft', 'Draft'), ('review', 'Under Review'), ('final', 'Final')]
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_investment_memos')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='investment_memos')
    deal_workspace = models.ForeignKey('private_equity.DueDiligenceRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='investment_memos')
    title = models.CharField(max_length=500, default='Investment Committee Memo')
    executive_summary = models.TextField(blank=True, default='')
    investment_thesis = models.TextField(blank=True, default='')
    market_overview = models.TextField(blank=True, default='')
    competitive_position = models.TextField(blank=True, default='')
    financial_overview = models.TextField(blank=True, default='')
    risk_assessment = models.JSONField(default=list, blank=True)
    key_terms = models.JSONField(default=list, blank=True)
    diligence_gaps = models.JSONField(default=list, blank=True)
    recommendation = models.TextField(blank=True, default='')
    confidence_level = models.CharField(max_length=20, default='medium', choices=[
        ('high', 'High'), ('medium', 'Medium'), ('low', 'Low'),
    ])
    next_steps = models.JSONField(default=list, blank=True)
    source_documents = models.ManyToManyField('core.File', blank=True, related_name='pe_investment_memos')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_investment_memo'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} - {self.company.name}"


# ============================================================================
# Solution 3: AI Commercial Due-Diligence Platform
# ============================================================================

class CommercialDDAssessment(models.Model):
    """Commercial due diligence assessment for a target company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_commercial_dd')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='commercial_assessments')
    deal_workspace = models.ForeignKey('private_equity.DueDiligenceRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='commercial_dd')
    market_size_estimate = models.CharField(max_length=255, blank=True, default='')
    market_growth_rate = models.CharField(max_length=100, blank=True, default='')
    competitive_landscape = models.JSONField(default=list, blank=True)
    customer_concentration_risk = models.CharField(max_length=20, default='medium', choices=[
        ('low', 'Low'), ('medium', 'Medium'), ('high', 'High'),
    ])
    pricing_power_assessment = models.TextField(blank=True, default='')
    demand_trend = models.TextField(blank=True, default='')
    churn_risk = models.CharField(max_length=20, default='medium', choices=[
        ('low', 'Low'), ('medium', 'Medium'), ('high', 'High'),
    ])
    growth_drivers = models.JSONField(default=list, blank=True)
    growth_headwinds = models.JSONField(default=list, blank=True)
    key_metrics = models.JSONField(default=dict, blank=True)
    overall_commercial_score = models.IntegerField(default=0, help_text="0-100")
    red_flags = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    source_files = models.ManyToManyField('core.File', blank=True, related_name='pe_commercial_dd')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_commercial_dd_assessment'
        ordering = ['-overall_commercial_score']

    def __str__(self):
        return f"Commercial DD - {self.company.name}"


# ============================================================================
# Solution 4: Quality-of-Earnings Analyzer
# ============================================================================

class EarningsAnalysis(models.Model):
    """Quality-of-earnings analysis for a portfolio company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_earnings_analyses')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='earnings_analyses')
    period_end = models.DateField(null=True, blank=True)
    adjusted_ebitda = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    adjustments = models.JSONField(default=list, blank=True)
    revenue_concentration = models.JSONField(default=dict, blank=True)
    working_capital_analysis = models.JSONField(default=dict, blank=True)
    one_time_items = models.JSONField(default=list, blank=True)
    recurring_revenue_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    earnings_quality_score = models.IntegerField(default=0, help_text="0-100")
    key_concerns = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    source_files = models.ManyToManyField('core.File', blank=True, related_name='pe_earnings_analyses')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_earnings_analysis'
        ordering = ['-period_end']

    def __str__(self):
        return f"QoE - {self.company.name} ({self.period_end})"


# ============================================================================
# Solution 5: AI Disruption and Readiness Scanner
# ============================================================================

class DisruptionAssessment(models.Model):
    """AI disruption assessment for a portfolio company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_disruption_assessments')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='disruption_assessments')
    overall_disruption_score = models.IntegerField(default=0, help_text="0-100, higher = more disrupted")
    product_impact = models.JSONField(default=dict, blank=True)
    pricing_impact = models.JSONField(default=dict, blank=True)
    workforce_impact = models.JSONField(default=dict, blank=True)
    competitive_moat_impact = models.JSONField(default=dict, blank=True)
    ai_opportunities = models.JSONField(default=list, blank=True)
    ai_threats = models.JSONField(default=list, blank=True)
    readiness_score = models.IntegerField(default=0, help_text="0-100")
    recommended_ai_investments = models.JSONField(default=list, blank=True)
    timeline_to_impact = models.CharField(max_length=20, default='medium', choices=[
        ('near', 'Near (< 1 year)'), ('medium', 'Medium (1-3 years)'), ('long', 'Long (3+ years)'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_disruption_assessment'
        ordering = ['-overall_disruption_score']

    def __str__(self):
        return f"Disruption - {self.company.name}"


# ============================================================================
# Solution 7: LBO and Valuation Scenario Copilot
# ============================================================================

class LBOScenario(models.Model):
    """LBO scenario modeling for a deal."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_lbo_scenarios')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='lbo_scenarios')
    deal_workspace = models.ForeignKey('private_equity.DueDiligenceRun', on_delete=models.SET_NULL, null=True, blank=True, related_name='lbo_scenarios')
    name = models.CharField(max_length=255, default='Base Case')
    entry_valuation = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    entry_multiple = models.DecimalField(max_digits=6, decimal_places=1, default=0)
    equity_contribution = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    debt_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    equity_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    exit_year = models.IntegerField(default=5)
    exit_multiple = models.DecimalField(max_digits=6, decimal_places=1, default=0)
    irr = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    moic = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    debt_paydown_schedule = models.JSONField(default=list, blank=True)
    covenant_headroom = models.JSONField(default=list, blank=True)
    sensitivity_table = models.JSONField(default=dict, blank=True)
    key_value_drivers = models.JSONField(default=list, blank=True)
    risk_factors = models.JSONField(default=list, blank=True)
    recommendation = models.TextField(blank=True, default='')
    assumptions = models.JSONField(default=dict, blank=True)
    projections = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_lbo_scenario'
        ordering = ['-irr']

    def __str__(self):
        return f"LBO {self.name} - {self.company.name}"


# ============================================================================
# Solution 8: Portfolio Early-Warning System
# ============================================================================

class PortfolioMetric(models.Model):
    """Time-series financial/KPI data per company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_portfolio_metrics')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='metrics')
    metric_name = models.CharField(max_length=255)
    metric_value = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    metric_unit = models.CharField(max_length=50, blank=True, default='')
    period_end = models.DateField()
    benchmark_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_portfolio_metric'
        ordering = ['-period_end']
        unique_together = ['company', 'metric_name', 'period_end']

    def __str__(self):
        return f"{self.company.name} - {self.metric_name}: {self.metric_value}"


class EarlyWarningAlert(models.Model):
    """Detected early-warning alerts for portfolio companies."""
    SEVERITY = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')]
    TREND = [('improving', 'Improving'), ('stable', 'Stable'), ('deteriorating', 'Deteriorating')]
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_early_warning_alerts')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='early_warning_alerts')
    metric = models.CharField(max_length=255)
    severity = models.CharField(max_length=20, choices=SEVERITY, default='medium')
    current_value = models.CharField(max_length=255, blank=True, default='')
    threshold = models.CharField(max_length=255, blank=True, default='')
    trend = models.CharField(max_length=20, choices=TREND, default='stable')
    description = models.TextField(blank=True, default='')
    recommended_action = models.TextField(blank=True, default='')
    is_acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    overall_risk_score = models.IntegerField(default=0, help_text="0-100")
    covenant_status = models.CharField(max_length=20, default='compliant', choices=[
        ('compliant', 'Compliant'), ('at_risk', 'At Risk'), ('breached', 'Breached'),
    ])
    forecast_outlook = models.CharField(max_length=20, default='stable', choices=[
        ('positive', 'Positive'), ('stable', 'Stable'),
        ('concerning', 'Concerning'), ('critical', 'Critical'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_early_warning_alert'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.company.name} - {self.metric} [{self.severity}]"


# ============================================================================
# Solution 9: Portfolio Value-Creation OS
# ============================================================================

class ValueCreationPlan(models.Model):
    """Value-creation plan (100-day plan) for a portfolio company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_vc_plans')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='vc_plans')
    fund = models.ForeignKey(Fund, on_delete=models.SET_NULL, null=True, blank=True, related_name='vc_plans')
    name = models.CharField(max_length=255, default='Value Creation Plan')
    investment_thesis = models.TextField(blank=True, default='')
    total_expected_ebitda_impact = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_headcount_impact = models.IntegerField(default=0)
    total_capex_required = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    quick_wins = models.JSONField(default=list, blank=True)
    strategic_initiatives = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, default='draft', choices=[
        ('draft', 'Draft'), ('active', 'Active'), ('completed', 'Completed'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_value_creation_plan'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.company.name}"


class ValueCreationInitiative(models.Model):
    """Individual initiative within a value-creation plan."""
    CATEGORIES = [
        ('revenue_growth', 'Revenue Growth'), ('cost_reduction', 'Cost Reduction'),
        ('working_capital', 'Working Capital'), ('capital_efficiency', 'Capital Efficiency'),
    ]
    STATUS_CHOICES = [
        ('not_started', 'Not Started'), ('in_progress', 'In Progress'),
        ('completed', 'Completed'), ('on_hold', 'On Hold'),
    ]
    PRIORITY = [('high', 'High'), ('medium', 'Medium'), ('low', 'Low')]

    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_vc_initiatives')
    plan = models.ForeignKey(ValueCreationPlan, on_delete=models.CASCADE, related_name='initiatives')
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORIES, default='revenue_growth')
    description = models.TextField(blank=True, default='')
    expected_annual_impact = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    actual_impact = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    implementation_timeline = models.CharField(max_length=100, blank=True, default='')
    owner_role = models.CharField(max_length=255, blank=True, default='')
    kpi_to_track = models.CharField(max_length=255, blank=True, default='')
    milestones = models.JSONField(default=list, blank=True)
    priority = models.CharField(max_length=20, choices=PRIORITY, default='medium')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='not_started')
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)
    completed_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_value_creation_initiative'
        ordering = ['priority', '-expected_annual_impact']

    def __str__(self):
        return f"{self.name} [{self.status}]"


# ============================================================================
# Solution 10: Cross-Portfolio Procurement Platform
# ============================================================================

class SpendClassification(models.Model):
    """Classified spend data across portfolio companies."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_spend_classifications')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='spend_data')
    supplier_name = models.CharField(max_length=255, blank=True, default='')
    category = models.CharField(max_length=255, blank=True, default='')
    sub_category = models.CharField(max_length=255, blank=True, default='')
    annual_spend = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    contract_end_date = models.DateField(null=True, blank=True)
    is_duplicate = models.BooleanField(default=False)
    duplicate_group = models.CharField(max_length=255, blank=True, default='')
    consolidation_opportunity = models.BooleanField(default=False)
    benchmark_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_spend_classification'
        ordering = ['-annual_spend']

    def __str__(self):
        return f"{self.supplier_name} - ${self.annual_spend}"


# ============================================================================
# Solution 11: Revenue and Pricing Optimisation Engine
# ============================================================================

class PricingRecommendation(models.Model):
    """AI-generated pricing recommendations for portfolio companies."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_pricing_recommendations')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='pricing_recommendations')
    customer_segment = models.CharField(max_length=255, blank=True, default='')
    current_approach = models.TextField(blank=True, default='')
    recommended_action = models.TextField(blank=True, default='')
    expected_revenue_impact = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    confidence = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount_analysis = models.JSONField(default=dict, blank=True)
    cross_sell_opportunities = models.JSONField(default=list, blank=True)
    churn_risk_customers = models.JSONField(default=list, blank=True)
    revenue_upside_potential = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    pricing_health_score = models.IntegerField(default=0, help_text="0-100")
    status = models.CharField(max_length=30, default='draft', choices=[
        ('draft', 'Draft'), ('approved', 'Approved'), ('implemented', 'Implemented'), ('rejected', 'Rejected'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_pricing_recommendation'
        ordering = ['-expected_revenue_impact']

    def __str__(self):
        return f"Pricing - {self.company.name} ({self.customer_segment})"


# ============================================================================
# Solution 12: LP Reporting and DDQ Copilot
# ============================================================================

class LPReport(models.Model):
    """Generated LP quarterly report."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_lp_reports')
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='lp_reports')
    period = models.CharField(max_length=50, blank=True, default='Current Quarter')
    report_narrative = models.TextField(blank=True, default='')
    portfolio_highlights = models.JSONField(default=list, blank=True)
    performance_summary = models.JSONField(default=dict, blank=True)
    key_deals_update = models.JSONField(default=list, blank=True)
    market_outlook = models.TextField(blank=True, default='')
    risk_factors = models.JSONField(default=list, blank=True)
    next_quarter_preview = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, default='draft', choices=[
        ('draft', 'Draft'), ('review', 'Under Review'), ('sent', 'Sent'),
    ])
    generated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    output_file = models.FileField(upload_to='pe_lp_reports/%Y/%m/%d/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_lp_report'
        ordering = ['-created_at']

    def __str__(self):
        return f"LP Report {self.period} - {self.fund.name}"


class DDQResponse(models.Model):
    """Automated DDQ (Due Diligence Questionnaire) responses."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_ddq_responses')
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='ddq_responses')
    question = models.TextField()
    answer = models.TextField(blank=True, default='')
    source = models.TextField(blank=True, default='', help_text="Data source for the answer")
    category = models.CharField(max_length=100, blank=True, default='')
    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_ddq_response'
        ordering = ['category', 'question']

    def __str__(self):
        return f"DDQ: {self.question[:60]}"


# ============================================================================
# Solution 13: Fund Cash-Flow and Liquidity Forecaster
# ============================================================================

class CashFlowForecast(models.Model):
    """Fund cash-flow forecast."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_cf_forecasts')
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name='cf_forecasts')
    name = models.CharField(max_length=255, default='Cash Flow Forecast')
    forecast_periods = models.JSONField(default=list, blank=True)
    scenarios = models.JSONField(default=list, blank=True)
    liquidity_analysis = models.JSONField(default=dict, blank=True)
    pacing_recommendations = models.JSONField(default=list, blank=True)
    risk_factors = models.JSONField(default=list, blank=True)
    market_assumptions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pe_cashflow_forecast'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.fund.name}"


# ============================================================================
# Solution 14: Exit-Readiness Platform
# ============================================================================

class ExitReadinessAssessment(models.Model):
    """Exit-readiness assessment for a portfolio company."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_exit_assessments')
    company = models.ForeignKey(PortfolioCompany, on_delete=models.CASCADE, related_name='exit_assessments')
    readiness_score = models.IntegerField(default=0, help_text="0-100")
    category_scores = models.JSONField(default=dict, blank=True)
    kpi_gaps = models.JSONField(default=list, blank=True)
    data_weaknesses = models.JSONField(default=list, blank=True)
    likely_buyer_questions = models.JSONField(default=list, blank=True)
    operational_improvements = models.JSONField(default=list, blank=True)
    recommended_exit_timeline = models.CharField(max_length=255, blank=True, default='')
    estimated_valuation_range = models.JSONField(default=dict, blank=True)
    priority_actions = models.JSONField(default=list, blank=True)
    exit_multiples = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_exit_readiness_assessment'
        ordering = ['-readiness_score']

    def __str__(self):
        return f"Exit Readiness - {self.company.name} ({self.readiness_score})"


# ============================================================================
# Solution 15: Private-Market Data Quality Platform
# ============================================================================

class DataQualityRun(models.Model):
    """A data quality check run across multiple sources."""
    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_dq_runs')
    fund = models.ForeignKey(Fund, on_delete=models.SET_NULL, null=True, blank=True, related_name='dq_runs')
    name = models.CharField(max_length=255, default='Data Quality Check')
    overall_quality_score = models.IntegerField(default=0, help_text="0-100")
    reconciliation_summary = models.JSONField(default=dict, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    sources_checked = models.IntegerField(default=0)
    issues_found = models.IntegerField(default=0)
    status = models.CharField(max_length=20, default='running', choices=[
        ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'pe_data_quality_run'
        ordering = ['-created_at']

    def __str__(self):
        return f"DQ Run - {self.name} ({self.overall_quality_score})"


class DataQualityIssue(models.Model):
    """Individual data quality issue found during a run."""
    ISSUE_TYPES = [
        ('missing', 'Missing'), ('inconsistent', 'Inconsistent'),
        ('duplicate', 'Duplicate'), ('invalid', 'Invalid'),
    ]
    SEVERITY = [('low', 'Low'), ('medium', 'Medium'), ('high', 'High')]

    client = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='pe_dq_issues')
    run = models.ForeignKey(DataQualityRun, on_delete=models.CASCADE, related_name='issues')
    source = models.CharField(max_length=255, blank=True, default='')
    field_name = models.CharField(max_length=255, blank=True, default='')
    issue_type = models.CharField(max_length=30, choices=ISSUE_TYPES, default='missing')
    severity = models.CharField(max_length=20, choices=SEVERITY, default='medium')
    description = models.TextField(blank=True, default='')
    suggested_fix = models.TextField(blank=True, default='')
    is_resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pe_data_quality_issue'
        ordering = ['-severity', 'field_name']

    def __str__(self):
        return f"{self.field_name} - {self.issue_type} [{self.severity}]"
