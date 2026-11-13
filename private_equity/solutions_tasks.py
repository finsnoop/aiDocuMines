"""
Celery tasks for PE AI-powered solutions.
All tasks route through the user-selected LLM via pe_ai_service.
llm_config (provider + model) is threaded from the frontend request
through the view → task → ai_service call chain.
"""
from celery import shared_task
import logging
from django.contrib.auth import get_user_model
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()


# ============================================================================
# Solution 1: Deal-Sourcing Intelligence
# ============================================================================

@shared_task(bind=True, max_retries=2)
def score_deal_target_task(self, target_id: int, thesis: str, user_id: int, llm_config=None):
    """Score a deal target against investment thesis using AI."""
    from .solutions_models import DealTarget
    from .pe_ai_service import ai_classify_target

    try:
        target = DealTarget.objects.get(id=target_id)
        user = User.objects.get(id=user_id)

        result = ai_classify_target(
            target_info={
                'name': target.name, 'sector': target.sector,
                'sub_sector': target.sub_sector, 'hq': target.headquarters,
                'employees': target.employee_count,
                'estimated_revenue': str(target.estimated_revenue),
                'source_type': target.source_type,
                'description': target.description if hasattr(target, 'description') else '',
            },
            thesis=thesis,
            llm_config=llm_config,
        )

        target.thesis_alignment_score = result.get('score', 0)
        target.ai_rationale = result.get('rationale', '')
        target.thesis_alignment_points = result.get('thesis_alignment', [])
        target.gaps = result.get('gaps', [])
        target.recommended_channel = result.get('estimated_sourcing_channel', '')
        target.status = 'screened'
        target.save()

        return {'status': 'completed', 'target_id': target_id, 'score': target.thesis_alignment_score}

    except Exception as e:
        logger.error(f"Failed to score target {target_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 2: Investment Committee Copilot
# ============================================================================

@shared_task(bind=True, max_retries=2)
def generate_investment_memo_task(self, memo_id: int, user_id: int, llm_config=None):
    """Generate investment memo using AI."""
    from .solutions_models import InvestmentMemo
    from .pe_ai_service import ai_generate_investment_memo

    try:
        memo = InvestmentMemo.objects.get(id=memo_id)
        user = User.objects.get(id=user_id)

        doc_summaries = []
        for f in memo.source_documents.all()[:20]:
            if hasattr(f, 'content') and f.content:
                doc_summaries.append(f"File: {f.filename}\n{f.content[:2000]}")
            else:
                doc_summaries.append(f"File: {f.filename}")

        risk_findings = []
        if memo.deal_workspace:
            from .models import RiskClause
            for rc in RiskClause.objects.filter(due_diligence_run=memo.deal_workspace)[:50]:
                risk_findings.append({
                    'clause_type': rc.clause_type,
                    'risk_level': rc.risk_level,
                    'text': rc.clause_text[:500],
                })

        result = ai_generate_investment_memo(
            deal_data={
                'company': memo.company.name,
                'sector': memo.company.sector,
                'thesis': memo.investment_thesis,
            },
            document_summaries=doc_summaries,
            risk_findings=risk_findings,
            llm_config=llm_config,
        )

        memo.executive_summary = result.get('executive_summary', '')
        memo.investment_thesis = result.get('investment_thesis', '')
        memo.market_overview = result.get('market_overview', '')
        memo.competitive_position = result.get('competitive_position', '')
        memo.financial_overview = result.get('financial_overview', '')
        memo.risk_assessment = result.get('risk_assessment', [])
        memo.key_terms = result.get('key_terms', [])
        memo.diligence_gaps = result.get('diligence_gaps', [])
        memo.recommendation = result.get('recommendation', '')
        memo.confidence_level = result.get('confidence_level', 'medium')
        memo.next_steps = result.get('next_steps', [])
        memo.status = 'draft'
        memo.save()

        return {'status': 'completed', 'memo_id': memo_id}

    except Exception as e:
        logger.error(f"Failed to generate memo {memo_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 3: Commercial Due-Diligence
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_commercial_dd_task(self, assessment_id: int, user_id: int, llm_config=None):
    """Run commercial DD assessment using AI."""
    from .solutions_models import CommercialDDAssessment
    from .pe_ai_service import ai_commercial_dd

    try:
        assessment = CommercialDDAssessment.objects.get(id=assessment_id)

        result = ai_commercial_dd(
            target_info={
                'name': assessment.company.name,
                'sector': assessment.company.sector,
                'employees': assessment.company.employee_count,
                'description': assessment.company.description,
            },
            llm_config=llm_config,
        )

        assessment.market_size_estimate = result.get('market_size_estimate', '')
        assessment.market_growth_rate = result.get('market_growth_rate', '')
        assessment.competitive_landscape = result.get('competitive_landscape', [])
        assessment.customer_concentration_risk = result.get('customer_concentration_risk', 'medium')
        assessment.pricing_power_assessment = result.get('pricing_power_assessment', '')
        assessment.demand_trend = result.get('demand_trend', '')
        assessment.churn_risk = result.get('churn_risk', 'medium')
        assessment.growth_drivers = result.get('growth_drivers', [])
        assessment.growth_headwinds = result.get('growth_headwinds', [])
        assessment.key_metrics = result.get('key_metrics', {})
        assessment.overall_commercial_score = result.get('overall_commercial_score', 0)
        assessment.red_flags = result.get('red_flags', [])
        assessment.recommendations = result.get('recommendations', [])
        assessment.save()

        return {'status': 'completed', 'assessment_id': assessment_id}

    except Exception as e:
        logger.error(f"Commercial DD failed for {assessment_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 4: Quality-of-Earnings
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_earnings_analysis_task(self, analysis_id: int, user_id: int, llm_config=None):
    """Run QoE analysis using AI."""
    from .solutions_models import EarningsAnalysis
    from .pe_ai_service import ai_quality_of_earnings

    try:
        analysis = EarningsAnalysis.objects.get(id=analysis_id)

        financial_data = {}
        for f in analysis.source_files.all()[:10]:
            financial_data[f.filename] = f.content[:5000] if hasattr(f, 'content') and f.content else ''

        result = ai_quality_of_earnings(
            financial_data=financial_data,
            llm_config=llm_config,
        )

        analysis.adjusted_ebitda = result.get('adjusted_ebitda', 0)
        analysis.adjustments = result.get('ebitda_adjustments', [])
        analysis.revenue_concentration = result.get('revenue_concentration', {})
        analysis.working_capital_analysis = result.get('working_capital_analysis', {})
        analysis.one_time_items = result.get('one_time_items', [])
        analysis.recurring_revenue_pct = result.get('recurring_revenue_pct', 0)
        analysis.earnings_quality_score = result.get('earnings_quality_score', 0)
        analysis.key_concerns = result.get('key_concerns', [])
        analysis.recommendations = result.get('recommendations', [])
        analysis.save()

        return {'status': 'completed', 'analysis_id': analysis_id}

    except Exception as e:
        logger.error(f"QoE analysis failed for {analysis_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 5: AI Disruption Scanner
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_disruption_assessment_task(self, assessment_id: int, user_id: int, llm_config=None):
    """Run AI disruption assessment."""
    from .solutions_models import DisruptionAssessment
    from .pe_ai_service import ai_disruption_assessment

    try:
        assessment = DisruptionAssessment.objects.get(id=assessment_id)

        result = ai_disruption_assessment(
            company_info={
                'name': assessment.company.name,
                'sector': assessment.company.sector,
                'employees': assessment.company.employee_count,
                'description': assessment.company.description,
            },
            llm_config=llm_config,
        )

        assessment.overall_disruption_score = result.get('overall_disruption_score', 0)
        assessment.product_impact = result.get('product_impact', {})
        assessment.pricing_impact = result.get('pricing_impact', {})
        assessment.workforce_impact = result.get('workforce_impact', {})
        assessment.competitive_moat_impact = result.get('competitive_moat_impact', {})
        assessment.ai_opportunities = result.get('ai_opportunities', [])
        assessment.ai_threats = result.get('ai_threats', [])
        assessment.readiness_score = result.get('readiness_score', 0)
        assessment.recommended_ai_investments = result.get('recommended_ai_investments', [])
        assessment.timeline_to_impact = result.get('timeline_to_impact', 'medium')
        assessment.save()

        return {'status': 'completed', 'assessment_id': assessment_id}

    except Exception as e:
        logger.error(f"Disruption assessment failed for {assessment_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 7: LBO & Valuation Scenario
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_lbo_scenario_task(self, scenario_id: int, user_id: int, llm_config=None):
    """Run LBO scenario modeling using AI."""
    from .solutions_models import LBOScenario
    from .pe_ai_service import ai_ipo_scenario

    try:
        scenario = LBOScenario.objects.get(id=scenario_id)

        result = ai_ipo_scenario(
            entry_assumptions=scenario.assumptions,
            financial_projections=scenario.projections,
            llm_config=llm_config,
        )

        scenario.entry_valuation = result.get('entry_valuation', 0)
        scenario.irr = result.get('irr', [{}])[0].get('irr', 0) if result.get('irr') else 0
        scenario.moic = result.get('moic', [{}])[0].get('moic', 0) if result.get('moic') else 0
        scenario.debt_paydown_schedule = result.get('debt_paydown_schedule', [])
        scenario.covenant_headroom = result.get('covenant_headroom', [])
        scenario.sensitivity_table = result.get('sensitivity_table', {})
        scenario.key_value_drivers = result.get('key_value_drivers', [])
        scenario.risk_factors = result.get('risk_factors', [])
        scenario.recommendation = result.get('recommendation', '')
        scenario.save()

        return {'status': 'completed', 'scenario_id': scenario_id}

    except Exception as e:
        logger.error(f"LBO scenario failed for {scenario_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 8: Portfolio Early-Warning System
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_early_warning_scan_task(self, company_id: int, user_id: int, llm_config=None):
    """Run early-warning scan for a portfolio company."""
    from .solutions_models import PortfolioCompany, EarlyWarningAlert
    from .pe_ai_service import ai_early_warning

    try:
        company = PortfolioCompany.objects.get(id=company_id)

        metrics = {}
        for m in company.metrics.all()[:50]:
            metrics[m.metric_name] = float(m.metric_value)

        result = ai_early_warning(
            company_metrics={'name': company.name, **metrics},
            llm_config=llm_config,
        )

        for alert_data in result.get('alerts', []):
            EarlyWarningAlert.objects.create(
                client=company.client,
                company=company,
                metric=alert_data.get('metric', ''),
                severity=alert_data.get('severity', 'medium'),
                current_value=str(alert_data.get('current_value', '')),
                threshold=str(alert_data.get('threshold', '')),
                trend=alert_data.get('trend', 'stable'),
                description=alert_data.get('description', ''),
                recommended_action=alert_data.get('recommended_action', ''),
                overall_risk_score=result.get('overall_risk_score', 0),
                covenant_status=result.get('covenant_status', 'compliant'),
                forecast_outlook=result.get('forecast_outlook', 'stable'),
            )

        return {'status': 'completed', 'company_id': company_id, 'alerts_created': len(result.get('alerts', []))}

    except Exception as e:
        logger.error(f"Early warning scan failed for {company_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 9: Value-Creation OS
# ============================================================================

@shared_task(bind=True, max_retries=2)
def generate_value_creation_plan_task(self, plan_id: int, user_id: int, llm_config=None):
    """Generate value-creation plan using AI."""
    from .solutions_models import ValueCreationPlan, ValueCreationInitiative
    from .pe_ai_service import ai_value_creation_plan

    try:
        plan = ValueCreationPlan.objects.get(id=plan_id)

        result = ai_value_creation_plan(
            investment_thesis=plan.investment_thesis,
            company_data={
                'name': plan.company.name,
                'sector': plan.company.sector,
                'employees': plan.company.employee_count,
            },
            current_initiatives=[],
            llm_config=llm_config,
        )

        plan.total_expected_ebitda_impact = result.get('total_expected_ebitda_impact', 0)
        plan.total_headcount_impact = result.get('total_headcount_impact', 0)
        plan.total_capex_required = result.get('total_capex_required', 0)
        plan.quick_wins = result.get('quick_wins', [])
        plan.strategic_initiatives = result.get('strategic_initiatives', [])
        plan.save()

        for init_data in result.get('initiatives', []):
            ValueCreationInitiative.objects.create(
                client=plan.client,
                plan=plan,
                name=init_data.get('name', ''),
                category=init_data.get('category', 'revenue_growth'),
                description=init_data.get('description', ''),
                expected_annual_impact=init_data.get('expected_annual_impact', 0),
                implementation_timeline=init_data.get('implementation_timeline', ''),
                owner_role=init_data.get('owner_role', ''),
                kpi_to_track=init_data.get('kpi_to_track', ''),
                milestones=init_data.get('milestones', []),
                priority=init_data.get('priority', 'medium'),
            )

        return {'status': 'completed', 'plan_id': plan_id}

    except Exception as e:
        logger.error(f"Value creation plan failed for {plan_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 10: Cross-Portfolio Procurement
# ============================================================================

@shared_task(bind=True, max_retries=2)
def analyze_spend_task(self, client_id: int, user_id: int, llm_config=None):
    """Analyze spend across portfolio companies."""
    from .solutions_models import SpendClassification, PortfolioCompany
    from .pe_ai_service import ai_spend_classification

    try:
        spend_data = []
        for sc in SpendClassification.objects.filter(client_id=client_id).select_related('company')[:500]:
            spend_data.append({
                'company': sc.company.name,
                'supplier': sc.supplier_name,
                'category': sc.category,
                'annual_spend': float(sc.annual_spend),
            })

        if not spend_data:
            return {'status': 'completed', 'message': 'No spend data to analyze'}

        result = ai_spend_classification(spend_data, llm_config=llm_config)

        for dup in result.get('duplicate_suppliers', []):
            SpendClassification.objects.filter(
                client_id=client_id,
                supplier_name__icontains=dup.get('supplier_name', ''),
            ).update(is_duplicate=True, duplicate_group=dup.get('supplier_name', ''))

        return {'status': 'completed', 'summary': result.get('consolidation_opportunities', [])}

    except Exception as e:
        logger.error(f"Spend analysis failed: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 11: Revenue & Pricing Optimizer
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_pricing_optimization_task(self, recommendation_id: int, user_id: int, llm_config=None):
    """Run pricing optimization for a portfolio company."""
    from .solutions_models import PricingRecommendation
    from .pe_ai_service import ai_pricing_optimization

    try:
        rec = PricingRecommendation.objects.get(id=recommendation_id)

        result = ai_pricing_optimization(
            customer_data=[{'segment': rec.customer_segment, 'approach': rec.current_approach}],
            revenue_data={'company': rec.company.name},
            llm_config=llm_config,
        )

        rec.pricing_health_score = result.get('pricing_health_score', 0)
        rec.recommended_action = str(result.get('recommendations', [{}])[0].get('recommended_action', ''))
        rec.expected_revenue_impact = result.get('revenue_upside_potential', 0)
        rec.discount_analysis = result.get('discount_analysis', {})
        rec.cross_sell_opportunities = result.get('cross_sell_opportunities', [])
        rec.churn_risk_customers = result.get('churn_risk_customers', [])
        rec.revenue_upside_potential = result.get('revenue_upside_potential', 0)
        rec.save()

        return {'status': 'completed', 'recommendation_id': recommendation_id}

    except Exception as e:
        logger.error(f"Pricing optimization failed for {recommendation_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 12: LP Reporting & DDQ Copilot
# ============================================================================

@shared_task(bind=True, max_retries=2)
def generate_lp_report_task(self, report_id: int, user_id: int, llm_config=None):
    """Generate LP quarterly report using AI."""
    from .solutions_models import LPReport
    from .pe_ai_service import ai_lp_report

    try:
        report = LPReport.objects.get(id=report_id)

        fund_data = {
            'name': report.fund.name,
            'vintage': report.fund.vintage_year,
            'size': str(report.fund.fund_size),
            'strategy': report.fund.strategy,
            'capital_called': str(report.fund.capital_called),
            'capital_distributed': str(report.fund.capital_distributed),
        }

        companies = []
        for c in report.fund.companies.all()[:20]:
            companies.append({'name': c.name, 'sector': c.sector})

        result = ai_lp_report(
            fund_data=fund_data,
            portfolio_summary={'companies': companies, 'count': len(companies)},
            period=report.period,
            llm_config=llm_config,
        )

        report.report_narrative = result.get('report_narrative', '')
        report.portfolio_highlights = result.get('portfolio_highlights', [])
        report.performance_summary = result.get('performance_summary', {})
        report.key_deals_update = result.get('key_deals_update', [])
        report.market_outlook = result.get('market_outlook', '')
        report.risk_factors = result.get('risk_factors', [])
        report.next_quarter_preview = result.get('next_quarter_preview', '')
        report.save()

        from .solutions_models import DDQResponse
        for ddq in result.get('ddq_responses', []):
            DDQResponse.objects.create(
                client=report.client,
                fund=report.fund,
                question=ddq.get('question', ''),
                answer=ddq.get('answer', ''),
                source=ddq.get('source', ''),
            )

        return {'status': 'completed', 'report_id': report_id}

    except Exception as e:
        logger.error(f"LP report generation failed for {report_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 13: Fund Cash-Flow Forecaster
# ============================================================================

@shared_task(bind=True, max_retries=2)
def generate_cashflow_forecast_task(self, forecast_id: int, user_id: int, llm_config=None):
    """Generate fund cash-flow forecast using AI."""
    from .solutions_models import CashFlowForecast
    from .pe_ai_service import ai_cashflow_forecast

    try:
        forecast = CashFlowForecast.objects.get(id=forecast_id)

        fund_data = {
            'name': forecast.fund.name,
            'size': str(forecast.fund.fund_size),
            'capital_called': str(forecast.fund.capital_called),
            'capital_distributed': str(forecast.fund.capital_distributed),
        }

        portfolio = []
        for c in forecast.fund.companies.all()[:20]:
            portfolio.append({'name': c.name, 'sector': c.sector})

        result = ai_cashflow_forecast(
            fund_commitments=fund_data,
            portfolio_data=portfolio,
            market_assumptions=forecast.market_assumptions,
            llm_config=llm_config,
        )

        forecast.forecast_periods = result.get('forecast_periods', [])
        forecast.scenarios = result.get('scenarios', [])
        forecast.liquidity_analysis = result.get('liquidity_analysis', {})
        forecast.pacing_recommendations = result.get('pacing_recommendations', [])
        forecast.risk_factors = result.get('risk_factors', [])
        forecast.save()

        return {'status': 'completed', 'forecast_id': forecast_id}

    except Exception as e:
        logger.error(f"Cash-flow forecast failed for {forecast_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 14: Exit-Readiness Platform
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_exit_readiness_task(self, assessment_id: int, user_id: int, llm_config=None):
    """Run exit-readiness assessment using AI."""
    from .solutions_models import ExitReadinessAssessment
    from .pe_ai_service import ai_exit_readiness

    try:
        assessment = ExitReadinessAssessment.objects.get(id=assessment_id)

        result = ai_exit_readiness(
            company_data={
                'name': assessment.company.name,
                'sector': assessment.company.sector,
                'employees': assessment.company.employee_count,
            },
            financial_metrics={},
            llm_config=llm_config,
        )

        assessment.readiness_score = result.get('readiness_score', 0)
        assessment.category_scores = result.get('exit_readiness_by_category', {})
        assessment.kpi_gaps = result.get('kpi_gaps', [])
        assessment.data_weaknesses = result.get('data_weaknesses', [])
        assessment.likely_buyer_questions = result.get('likely_buyer_questions', [])
        assessment.operational_improvements = result.get('operational_improvements_needed', [])
        assessment.recommended_exit_timeline = result.get('recommended_exit_timeline', '')
        assessment.estimated_valuation_range = result.get('estimated_valuation_range', {})
        assessment.priority_actions = result.get('priority_actions', [])
        assessment.save()

        return {'status': 'completed', 'assessment_id': assessment_id}

    except Exception as e:
        logger.error(f"Exit readiness failed for {assessment_id}: {e}")
        raise self.retry(exc=e, countdown=60)


# ============================================================================
# Solution 15: Data Quality Platform
# ============================================================================

@shared_task(bind=True, max_retries=2)
def run_data_quality_check_task(self, run_id: int, user_id: int, llm_config=None):
    """Run data quality check using AI."""
    from .solutions_models import DataQualityRun, DataQualityIssue
    from .pe_ai_service import ai_data_quality_check

    try:
        dq_run = DataQualityRun.objects.get(id=run_id)

        data_sources = []
        from .solutions_models import FinancialRecord
        for fr in FinancialRecord.objects.filter(
            client=dq_run.client
        ).select_related('company')[:100]:
            data_sources.append({
                'company': fr.company.name,
                'period': str(fr.period_end),
                'revenue': float(fr.revenue),
                'ebitda': float(fr.ebitda),
                'net_income': float(fr.net_income),
                'total_assets': float(fr.total_assets),
            })

        result = ai_data_quality_check(data_sources=data_sources, llm_config=llm_config)

        dq_run.overall_quality_score = result.get('overall_quality_score', 0)
        dq_run.reconciliation_summary = result.get('reconciliation_summary', {})
        dq_run.recommendations = result.get('recommendations', [])
        dq_run.sources_checked = len(data_sources)
        dq_run.status = 'completed'
        dq_run.completed_at = timezone.now()
        dq_run.save()

        issues_created = 0
        for issue_data in result.get('issues', []):
            DataQualityIssue.objects.create(
                client=dq_run.client,
                run=dq_run,
                source=issue_data.get('source', ''),
                field_name=issue_data.get('field', ''),
                issue_type=issue_data.get('issue_type', 'missing'),
                severity=issue_data.get('severity', 'medium'),
                description=issue_data.get('description', ''),
                suggested_fix=issue_data.get('suggested_fix', ''),
            )
            issues_created += 1

        dq_run.issues_found = issues_created
        dq_run.save()

        return {'status': 'completed', 'run_id': run_id, 'issues': issues_created}

    except Exception as e:
        logger.error(f"Data quality check failed for {run_id}: {e}")
        dq_run.status = 'failed'
        dq_run.save()
        raise self.retry(exc=e, countdown=60)
