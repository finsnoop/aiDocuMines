"""
DSPy Signatures for all 15 PE AI-Powered Solutions.
Each signature declares structured inputs/outputs — DSPy enforces the schema at inference.
"""
import dspy
from typing import Literal


# ============================================================================
# Existing Signatures (Document Classification, Risk Extraction, etc.)
# ============================================================================

class ClassifyPEDocument(dspy.Signature):
    """Classify a document in the context of Private Equity due diligence."""
    document_text = dspy.InputField(desc="The full text of the document to classify")
    deal_name = dspy.InputField(desc="Name of the M&A deal or transaction", default="")
    target_company = dspy.InputField(desc="Name of the target company", default="")

    document_type = dspy.OutputField(desc="Type of document (e.g., 'nda', 'supplier_contract', 'financial_statement')")
    sub_type = dspy.OutputField(desc="More specific sub-type if applicable")
    confidence_score = dspy.OutputField(desc="Confidence score between 0.0 and 1.0")
    key_indicators = dspy.OutputField(desc="List of key phrases that led to this classification")
    relevance_to_dd = dspy.OutputField(desc="Relevance to due diligence (0.0 to 1.0)")
    priority = dspy.OutputField(desc="Review priority: critical, high, medium, or low")
    recommended_reviewers = dspy.OutputField(desc="Comma-separated reviewer roles (e.g., 'legal, financial, technical')")
    reasoning = dspy.OutputField(desc="Explanation of the classification decision")


class ExtractPERiskClauses(dspy.Signature):
    """Extract and analyze risky clauses from a PE due diligence document."""
    document_text = dspy.InputField(desc="The full text of the document to analyze")
    document_type = dspy.InputField(desc="Type of document being analyzed", default="")
    deal_type = dspy.InputField(desc="Type of deal: acquisition, merger, investment, or divestiture", default="acquisition")
    target_company = dspy.InputField(desc="Name of the target company", default="")
    focus_areas = dspy.InputField(desc="Specific risk areas to focus on (comma-separated)", default="")

    risk_clauses_json = dspy.OutputField(desc="JSON array of risk clauses, each with: clause_text, risk_level, risk_category, impact_description, mitigation_suggestions, page_number, section")
    overall_risk_score = dspy.OutputField(desc="Overall risk score for the document (0.0 to 1.0)")
    summary = dspy.OutputField(desc="Executive summary of the risks found")
    deal_impact = dspy.OutputField(desc="Overall impact on the deal")
    recommendations = dspy.OutputField(desc="High-level recommendations for addressing the risks")


class GenerateDDFindings(dspy.Signature):
    """Generate comprehensive due diligence findings report from analyzed documents."""
    deal_name = dspy.InputField(desc="Name of the M&A deal or transaction")
    target_company = dspy.InputField(desc="Name of the target company")
    deal_type = dspy.InputField(desc="Type of deal: acquisition, merger, investment, or divestiture")
    document_summary = dspy.InputField(desc="JSON summary of classified documents with counts by type")
    risk_summary = dspy.InputField(desc="JSON summary of risk clauses by category and severity")
    key_documents = dspy.InputField(desc="List of key documents reviewed (comma-separated)")
    focus_areas = dspy.InputField(desc="Specific areas to focus on in the report", default="")

    executive_summary = dspy.OutputField(desc="Executive summary of the due diligence findings (2-3 paragraphs)")
    key_findings_json = dspy.OutputField(desc="JSON array of key findings, each with: title, description, severity, category, supporting_documents, recommendations")
    recommendations_json = dspy.OutputField(desc="JSON array of recommendations, each with: title, description, priority, action_items, responsible_party")
    overall_risk_assessment = dspy.OutputField(desc="Overall risk assessment (low, medium, high, critical)")
    deal_recommendation = dspy.OutputField(desc="Recommendation on whether to proceed with the deal, with detailed reasoning")
    confidence_score = dspy.OutputField(desc="Confidence in the analysis (0.0 to 1.0)")
    reasoning = dspy.OutputField(desc="Explanation of how the findings were derived")


class AnswerPEQuestion(dspy.Signature):
    """Answer questions about documents in the context of PE due diligence."""
    question = dspy.InputField(desc="The question to answer")
    document_context = dspy.InputField(desc="Relevant document excerpts and metadata")
    deal_name = dspy.InputField(desc="Name of the M&A deal", default="")
    target_company = dspy.InputField(desc="Name of the target company", default="")

    answer = dspy.OutputField(desc="Detailed answer to the question")
    confidence_score = dspy.OutputField(desc="Confidence in the answer (0.0 to 1.0)")
    source_documents = dspy.OutputField(desc="List of source documents used to answer the question")
    relevant_excerpts = dspy.OutputField(desc="Relevant excerpts from the documents that support the answer")
    follow_up_questions = dspy.OutputField(desc="Suggested follow-up questions")


class ExtractPEKeyInfo(dspy.Signature):
    """Extract key information from PE documents: parties, dates, amounts, terms."""
    document_text = dspy.InputField(desc="The full text of the document")
    document_type = dspy.InputField(desc="Type of document", default="")
    extraction_fields = dspy.InputField(desc="Specific fields to extract (comma-separated)", default="parties,effective_date,termination_date,contract_value,key_terms")

    key_values_json = dspy.OutputField(desc="JSON array of extracted key-value pairs, each with: key, value, confidence, source_text")
    summary = dspy.OutputField(desc="Summary of the key information extracted")
    completeness_score = dspy.OutputField(desc="How complete the extraction is (0.0 to 1.0)")


# ============================================================================
# Solution 1: Deal-Sourcing Intelligence
# ============================================================================

DealType = Literal["carve_out", "succession", "consolidation", "add_on", "platform"]

class ScoreDealTarget(dspy.Signature):
    """Score how well a deal target matches an investment thesis."""
    target_info = dspy.InputField(desc="JSON with target company details: name, sector, sub_sector, hq, employees, estimated_revenue, source_type, description")
    investment_thesis = dspy.InputField(desc="The fund's investment thesis text")

    score = dspy.OutputField(desc="Thesis alignment score from 0 to 100")
    rationale = dspy.OutputField(desc="Detailed explanation of the score")
    thesis_alignment = dspy.OutputField(desc="JSON list of points where target aligns with thesis")
    gaps = dspy.OutputField(desc="JSON list of gaps or misalignment areas")
    sector = dspy.OutputField(desc="Primary sector classification")
    sub_sector = dspy.OutputField(desc="Sub-sector classification")
    deal_type: DealType = dspy.OutputField(desc="Recommended deal type")
    estimated_sourcing_channel = dspy.OutputField(desc="Best sourcing channel for this target")


# ============================================================================
# Solution 2: IC Copilot
# ============================================================================

ICRecommendation = Literal["proceed", "conditional", "pass"]
ConfidenceLevel = Literal["high", "medium", "low"]

class GenerateInvestmentMemo(dspy.Signature):
    """Generate a comprehensive investment committee memo with cited sections."""
    deal_data = dspy.InputField(desc="JSON with deal details: company name, sector, thesis")
    document_summaries = dspy.InputField(desc="Semicolon-separated document summaries from diligence")
    risk_findings = dspy.InputField(desc="JSON array of risk findings from diligence")

    executive_summary = dspy.OutputField(desc="Executive summary (2-3 paragraphs)")
    investment_thesis = dspy.OutputField(desc="The investment thesis statement and rationale")
    market_overview = dspy.OutputField(desc="Market overview with size, growth, dynamics")
    competitive_position = dspy.OutputField(desc="Competitive positioning analysis")
    financial_overview = dspy.OutputField(desc="Financial summary with key metrics")
    risk_assessment = dspy.OutputField(desc="JSON array of risks with severity levels and citations")
    key_terms = dspy.OutputField(desc="JSON array of key deal terms")
    diligence_gaps = dspy.OutputField(desc="JSON array of open questions requiring further diligence")
    recommendation: ICRecommendation = dspy.OutputField(desc="IC recommendation")
    confidence_level: ConfidenceLevel = dspy.OutputField(desc="Confidence in the recommendation")
    next_steps = dspy.OutputField(desc="JSON array of next steps before IC vote")


# ============================================================================
# Solution 3: Commercial DD Platform
# ============================================================================

DemandTrend = Literal["accelerating", "stable", "declining"]
ChurnRisk = Literal["low", "medium", "high"]

class RunCommercialDD(dspy.Signature):
    """Structured commercial due diligence: market sizing, competition, growth drivers."""
    target_info = dspy.InputField(desc="JSON with target company details: name, sector, employees, description")
    market_data = dspy.InputField(desc="JSON market data if available", default="")
    document_texts = dspy.InputField(desc="Semicolon-separated relevant document excerpts", default="")

    market_size_estimate = dspy.OutputField(desc="Estimated TAM/SAM/SOM with methodology")
    market_growth_rate = dspy.OutputField(desc="Market CAGR and growth projections")
    competitive_landscape = dspy.OutputField(desc="JSON array of competitors with positioning and market share")
    customer_concentration_risk = dspy.OutputField(desc="HHI index and top customer dependency analysis")
    pricing_power_assessment = dspy.OutputField(desc="Assessment of pricing power and elasticity")
    demand_trend: DemandTrend = dspy.OutputField(desc="Demand trajectory")
    churn_risk: ChurnRisk = dspy.OutputField(desc="Customer churn risk level")
    growth_drivers = dspy.OutputField(desc="JSON list of key growth drivers")
    growth_headwinds = dspy.OutputField(desc="JSON list of growth headwinds and risks")
    key_metrics = dspy.OutputField(desc="JSON dict of key commercial metrics")
    overall_commercial_score = dspy.OutputField(desc="Commercial attractiveness score from 0 to 100")
    red_flags = dspy.OutputField(desc="JSON list of commercial red flags")
    recommendations = dspy.OutputField(desc="JSON list of recommendations")


# ============================================================================
# Solution 4: Quality-of-Earnings
# ============================================================================

QualityFlag = Literal["normalized", "questionable", "aggressive"]

class RunEarningsAnalysis(dspy.Signature):
    """Normalize EBITDA, identify add-backs, flag one-time items, validate performance."""
    financial_data = dspy.InputField(desc="JSON with financial data: revenue, EBITDA, net income, margins")
    trial_balance = dspy.InputField(desc="JSON array of trial balance line items", default="")

    adjusted_ebitda = dspy.OutputField(desc="Calculated adjusted EBITDA figure")
    ebitda_adjustments = dspy.OutputField(desc="JSON array of adjustments with: item, amount, justification, quality_flag (normalized/questionable/aggressive)")
    revenue_concentration = dspy.OutputField(desc="JSON dict of revenue by segment/customer with concentration %")
    working_capital_analysis = dspy.OutputField(desc="JSON dict with working capital trends and analysis")
    one_time_items = dspy.OutputField(desc="JSON list of one-time or non-recurring items identified")
    recurring_revenue_pct = dspy.OutputField(desc="Percentage of revenue that is recurring")
    earnings_quality_score = dspy.OutputField(desc="Earnings quality score from 0 to 100")
    key_concerns = dspy.OutputField(desc="JSON list of key concerns for the IC")
    recommendations = dspy.OutputField(desc="JSON list of recommendations")


# ============================================================================
# Solution 5: AI Disruption Scanner
# ============================================================================

TimelineToImpact = Literal["near", "medium", "long"]

class RunDisruptionAssessment(dspy.Signature):
    """Assess AI/automation threats to portfolio company revenue streams."""
    company_info = dspy.InputField(desc="JSON with company details: name, sector, employees, description")
    industry_context = dspy.InputField(desc="Industry context and competitive dynamics", default="")

    overall_disruption_score = dspy.OutputField(desc="Disruption risk score from 0 to 100 (higher = more disrupted)")
    product_impact = dspy.OutputField(desc="JSON with score and description of AI impact on products")
    pricing_impact = dspy.OutputField(desc="JSON with score and description of AI impact on pricing")
    workforce_impact = dspy.OutputField(desc="JSON with score and description of AI impact on workforce")
    competitive_moat_impact = dspy.OutputField(desc="JSON with score and description of AI impact on competitive moat")
    ai_opportunities = dspy.OutputField(desc="JSON list of AI opportunities with area, impact, timeline")
    ai_threats = dspy.OutputField(desc="JSON list of AI threats with area, severity, timeline")
    readiness_score = dspy.OutputField(desc="AI readiness score from 0 to 100")
    recommended_ai_investments = dspy.OutputField(desc="JSON list of recommended AI investments")
    timeline_to_impact: TimelineToImpact = dspy.OutputField(desc="Timeline to significant disruption impact")


# ============================================================================
# Solution 7: LBO & Valuation Scenario
# ============================================================================

class RunLBOScenario(dspy.Signature):
    """Build LBO model with IRR/MOIC projections and sensitivity analysis."""
    entry_assumptions = dspy.InputField(desc="JSON with entry assumptions: enterprise_value, equity_check, debt, growth rates, exit multiple")
    financial_projections = dspy.InputField(desc="JSON with financial projections: revenue, EBITDA, capex, working capital")

    entry_valuation = dspy.OutputField(desc="Entry enterprise value and equity value")
    irr = dspy.OutputField(desc="JSON array of IRR scenarios (base, bull, bear)")
    moic = dspy.OutputField(desc="JSON array of MOIC scenarios")
    debt_paydown_schedule = dspy.OutputField(desc="JSON array of {year, debt_balance, fcf} for debt amortization")
    covenant_headroom = dspy.OutputField(desc="JSON array of covenant compliance analysis")
    sensitivity_table = dspy.OutputField(desc="JSON dict: IRR by entry multiple vs exit multiple")
    key_value_drivers = dspy.OutputField(desc="JSON list of value drivers with driver name and impact")
    risk_factors = dspy.OutputField(desc="JSON list of key risk factors")
    recommendation = dspy.OutputField(desc="Modeling recommendation and key takeaways")


# ============================================================================
# Solution 8: Portfolio Early-Warning System
# ============================================================================

CovenantStatus = Literal["compliant", "at_risk", "breached"]
ForecastOutlook = Literal["positive", "stable", "concerning", "critical"]
Severity = Literal["low", "medium", "high", "critical"]

class RunEarlyWarningScan(dspy.Signature):
    """Analyze portfolio KPIs and detect early warning signals."""
    company_metrics = dspy.InputField(desc="JSON dict of current metric values: revenue, ebitda, margins, etc.")
    historical_data = dspy.InputField(desc="JSON array of historical metric snapshots", default="")
    covenant_data = dspy.InputField(desc="JSON dict of covenant thresholds and current headroom", default="")

    overall_risk_score = dspy.OutputField(desc="Overall risk score from 0 to 100")
    alerts = dspy.OutputField(desc="JSON array of alerts with: metric, severity (low/medium/high/critical), current_value, threshold, trend (improving/stable/deteriorating), description, recommended_action")
    covenant_status: CovenantStatus = dspy.OutputField(desc="Current covenant compliance status")
    forecast_outlook: ForecastOutlook = dspy.OutputField(desc="Portfolio outlook assessment")
    key_metrics_trend = dspy.OutputField(desc="JSON dict of metric name to trend direction")


# ============================================================================
# Solution 9: Value-Creation OS
# ============================================================================

InitiativeCategory = Literal["revenue_growth", "cost_reduction", "working_capital", "capital_efficiency"]
Priority = Literal["high", "medium", "low"]

class GenerateValueCreationPlan(dspy.Signature):
    """Create a value-creation plan with 100-day playbooks and OKRs."""
    investment_thesis = dspy.InputField(desc="The investment thesis and value creation rationale")
    company_data = dspy.InputField(desc="JSON with company details: name, sector, employees, current EBITDA")
    current_initiatives = dspy.InputField(desc="JSON array of any existing initiatives", default="")

    plan_name = dspy.OutputField(desc="Name for the value creation plan")
    total_expected_ebitda_impact = dspy.OutputField(desc="Total expected annual EBITDA impact (currency)")
    initiatives = dspy.OutputField(desc="JSON array of initiatives with: name, category (revenue_growth/cost_reduction/working_capital/capital_efficiency), description, expected_annual_impact, implementation_timeline, owner_role, kpi_to_track, milestones (list), priority (high/medium/low)")
    quick_wins = dspy.OutputField(desc="JSON list of quick wins implementable in 30-60 days")
    strategic_initiatives = dspy.OutputField(desc="JSON list of longer-term strategic initiatives")
    total_headcount_impact = dspy.OutputField(desc="Net headcount impact (positive = hires, negative = reductions)")
    total_capex_required = dspy.OutputField(desc="Total capital expenditure required for all initiatives")


# ============================================================================
# Solution 10: Cross-Portfolio Procurement
# ============================================================================

class AnalyzeSpend(dspy.Signature):
    """Aggregate spend across portfolio companies to find consolidation opportunities."""
    spend_data = dspy.InputField(desc="JSON array of spend records with: company, supplier, category, annual_spend")

    total_spend = dspy.OutputField(desc="Total aggregate spend across all companies")
    spend_by_category = dspy.OutputField(desc="JSON dict of spend totals by category")
    duplicate_suppliers = dspy.OutputField(desc="JSON array of duplicate suppliers with: supplier_name, companies_using, spend_range, consolidation_opportunity")
    consolidation_opportunities = dspy.OutputField(desc="JSON array with: category, potential_savings, recommended_suppliers, confidence")
    spend_anomalies = dspy.OutputField(desc="JSON list of spend anomalies detected")
    benchmark_comparison = dspy.OutputField(desc="JSON dict comparing spend to industry benchmarks")


# ============================================================================
# Solution 11: Revenue & Pricing Optimizer
# ============================================================================

class RunPricingOptimization(dspy.Signature):
    """Benchmark pricing, identify revenue leakage, recommend optimization strategies."""
    customer_data = dspy.InputField(desc="JSON array of customer segment data with pricing and behavior")
    revenue_data = dspy.InputField(desc="JSON dict with revenue breakdowns and trends", default="")

    pricing_health_score = dspy.OutputField(desc="Pricing health score from 0 to 100")
    recommendations = dspy.OutputField(desc="JSON array with: customer_segment, current_approach, recommended_action, expected_revenue_impact, confidence")
    discount_analysis = dspy.OutputField(desc="JSON dict analyzing discount patterns and leakage")
    cross_sell_opportunities = dspy.OutputField(desc="JSON list of cross-sell and upsell opportunities")
    churn_risk_customers = dspy.OutputField(desc="JSON array with: customer, risk_factors, intervention")
    revenue_upside_potential = dspy.OutputField(desc="Estimated annual revenue upside from optimization (currency)")


# ============================================================================
# Solution 12: LP Reporting & DDQ Copilot
# ============================================================================

class GenerateLPReport(dspy.Signature):
    """Generate quarterly LP report and draft DDQ responses from fund data."""
    fund_data = dspy.InputField(desc="JSON with fund details: name, vintage, size, strategy, capital called, distributions")
    portfolio_summary = dspy.InputField(desc="JSON with portfolio company list and aggregate stats")
    period = dspy.InputField(desc="Reporting period (e.g., Q4 2025)")

    report_narrative = dspy.OutputField(desc="Full quarterly report narrative (3-5 paragraphs)")
    portfolio_highlights = dspy.OutputField(desc="JSON list of key portfolio company highlights")
    performance_summary = dspy.OutputField(desc="JSON dict with IRR, TVPI, DPI, RVPI")
    key_deals_update = dspy.OutputField(desc="JSON list of key deal updates during the period")
    market_outlook = dspy.OutputField(desc="Market outlook paragraph")
    risk_factors = dspy.OutputField(desc="JSON list of risk factors to disclose to LPs")
    ddq_responses = dspy.OutputField(desc="JSON array of {question, answer, source} for standard DDQ items")
    next_quarter_preview = dspy.OutputField(desc="Preview of next quarter activities")


# ============================================================================
# Solution 13: Fund Cash-Flow Forecaster
# ============================================================================

ScenarioName = Literal["base", "bull", "bear"]

class GenerateCashFlowForecast(dspy.Signature):
    """Model capital calls, distributions, and LP cash-flow exposure."""
    fund_commitments = dspy.InputField(desc="JSON with fund details: name, size, capital called, distributions")
    portfolio_data = dspy.InputField(desc="JSON array of portfolio company data")
    market_assumptions = dspy.InputField(desc="JSON dict of market assumptions for scenarios", default="")

    forecast_periods = dspy.OutputField(desc="JSON array of {period, capital_call, distribution, nav, uncalled_commitment}")
    scenarios = dspy.OutputField(desc="JSON array of {name (base/bull/bear), irr, tvpi, total_calls, total_distributions}")
    liquidity_analysis = dspy.OutputField(desc="JSON dict with liquidity position and runway analysis")
    pacing_recommendations = dspy.OutputField(desc="JSON list of pacing recommendations")
    risk_factors = dspy.OutputField(desc="JSON list of risk factors affecting the forecast")


# ============================================================================
# Solution 14: Exit-Readiness Platform
# ============================================================================

class RunExitReadiness(dspy.Signature):
    """Readiness scoring, buyer-matching, and exit-timing analysis."""
    company_data = dspy.InputField(desc="JSON with company details: name, sector, employees, ownership stake")
    financial_metrics = dspy.InputField(desc="JSON with financial KPIs: revenue, EBITDA, margins, growth")

    readiness_score = dspy.OutputField(desc="Overall exit readiness score from 0 to 100")
    kpi_gaps = dspy.OutputField(desc="JSON array of {kpi, current, benchmark, gap}")
    data_weaknesses = dspy.OutputField(desc="JSON list of data weaknesses that could deter buyers")
    likely_buyer_questions = dspy.OutputField(desc="JSON list of questions buyers will likely ask")
    operational_improvements = dspy.OutputField(desc="JSON array of {area, impact, timeline} needed before exit")
    recommended_exit_timeline = dspy.OutputField(desc="Recommended exit timing (e.g., '6-12 months after operational improvements')")
    estimated_valuation_range = dspy.OutputField(desc="JSON dict with low, mid, high valuation estimates")
    exit_readiness_by_category = dspy.OutputField(desc="JSON dict with financial, operational, commercial, legal, governance scores")
    priority_actions = dspy.OutputField(desc="JSON list of top priority actions ranked by impact")


# ============================================================================
# Solution 15: Data Quality Platform
# ============================================================================

IssueType = Literal["missing", "inconsistent", "duplicate", "invalid"]

class RunDataQualityCheck(dspy.Signature):
    """Monitor data freshness, completeness, accuracy across portfolio data feeds."""
    data_sources = dspy.InputField(desc="JSON array of data records to validate across sources")
    schema_definition = dspy.InputField(desc="JSON dict defining expected schema and constraints", default="")

    overall_quality_score = dspy.OutputField(desc="Overall data quality score from 0 to 100")
    issues = dspy.OutputField(desc="JSON array of issues with: source, field, issue_type (missing/inconsistent/duplicate/invalid), severity, description, suggested_fix")
    schema_matches = dspy.OutputField(desc="JSON array of {source, match_score, mismatches}")
    reconciliation_summary = dspy.OutputField(desc="JSON dict summarizing cross-source reconciliation results")
    recommendations = dspy.OutputField(desc="JSON list of prioritized remediation recommendations")
