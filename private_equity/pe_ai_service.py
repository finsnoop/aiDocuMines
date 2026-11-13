"""
Centralized AI Service Layer for PE Solutions — DSPy 3.x.
Every AI call receives the user's llm_config (provider + model) and creates
a per-call DSPy LM via the factory in llm_service.py.  No model is hardcoded.
"""
import json
import logging
from typing import Optional, Dict, Any, List

import dspy

from .llm_service import make_lm
from .dspy_signatures import (
    ScoreDealTarget,
    GenerateInvestmentMemo,
    RunCommercialDD,
    RunEarningsAnalysis,
    RunDisruptionAssessment,
    RunLBOScenario,
    RunEarlyWarningScan,
    GenerateValueCreationPlan,
    AnalyzeSpend,
    RunPricingOptimization,
    GenerateLPReport,
    GenerateCashFlowForecast,
    RunExitReadiness,
    RunDataQualityCheck,
)

logger = logging.getLogger(__name__)


def _result_to_dict(result: dspy.Prediction) -> Dict[str, Any]:
    """Convert a DSPy Prediction to a plain dict for backward compatibility."""
    return {k: v for k, v in result.items()}


def _call_with_config(signature_cls, llm_config: Optional[Dict] = None, **inputs) -> Dict[str, Any]:
    """
    Create a ChainOfThought module with the user's chosen LM and invoke it.

    Parameters
    ----------
    signature_cls : dspy.Signature subclass
        The DSPy signature to use.
    llm_config : dict, optional
        The user's LLM config {provider, model, api_key?, endpoint?}.
        Falls back to the platform-global default if not provided.
    **inputs
        Keyword arguments matching the signature's input fields.

    Returns
    -------
    dict — the prediction fields as a plain dictionary.
    """
    lm = make_lm(llm_config)
    module = dspy.ChainOfThought(signature_cls)
    with dspy.settings(lm=lm):
        result = module(**inputs)
    return _result_to_dict(result)


# ============================================================================
# Solution 1: Deal-Sourcing Intelligence
# ============================================================================

def ai_classify_target(
    target_info: Dict[str, Any],
    thesis: str,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Score a deal target against the investment thesis."""
    return _call_with_config(
        ScoreDealTarget, llm_config,
        target_info=json.dumps(target_info, default=str),
        investment_thesis=thesis,
    )


# ============================================================================
# Solution 2: IC Copilot
# ============================================================================

def ai_generate_investment_memo(
    deal_data: Dict[str, Any],
    document_summaries: List[str],
    risk_findings: List[Dict],
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Generate a cited investment committee memo."""
    return _call_with_config(
        GenerateInvestmentMemo, llm_config,
        deal_data=json.dumps(deal_data, default=str),
        document_summaries="; ".join(document_summaries[:20]),
        risk_findings=json.dumps(risk_findings[:50], default=str),
    )


# ============================================================================
# Solution 3: Commercial DD Platform
# ============================================================================

def ai_commercial_dd(
    target_info: Dict[str, Any],
    market_data: Optional[Dict] = None,
    document_texts: Optional[List[str]] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Structured commercial due diligence assessment."""
    return _call_with_config(
        RunCommercialDD, llm_config,
        target_info=json.dumps(target_info, default=str),
        market_data=json.dumps(market_data, default=str) if market_data else "",
        document_texts="; ".join(document_texts[:10]) if document_texts else "",
    )


# ============================================================================
# Solution 4: Quality-of-Earnings
# ============================================================================

def ai_quality_of_earnings(
    financial_data: Dict[str, Any],
    trial_balance: Optional[List[Dict]] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Normalize EBITDA, identify add-backs, flag one-time items."""
    return _call_with_config(
        RunEarningsAnalysis, llm_config,
        financial_data=json.dumps(financial_data, default=str),
        trial_balance=json.dumps(trial_balance[:200] if trial_balance else [], default=str),
    )


# ============================================================================
# Solution 5: AI Disruption Scanner
# ============================================================================

def ai_disruption_assessment(
    company_info: Dict[str, Any],
    industry_context: Optional[str] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Assess AI/automation threats to portfolio company revenue streams."""
    return _call_with_config(
        RunDisruptionAssessment, llm_config,
        company_info=json.dumps(company_info, default=str),
        industry_context=industry_context or "",
    )


# ============================================================================
# Solution 7: LBO & Valuation Scenario
# ============================================================================

def ai_ipo_scenario(
    entry_assumptions: Dict[str, Any],
    financial_projections: Dict[str, Any],
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Build LBO model with IRR/MOIC projections and sensitivity analysis."""
    return _call_with_config(
        RunLBOScenario, llm_config,
        entry_assumptions=json.dumps(entry_assumptions, default=str),
        financial_projections=json.dumps(financial_projections, default=str),
    )


# ============================================================================
# Solution 8: Portfolio Early-Warning System
# ============================================================================

def ai_early_warning(
    company_metrics: Dict[str, Any],
    historical_data: Optional[List[Dict]] = None,
    covenant_data: Optional[Dict] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Analyze portfolio KPIs and detect early warning signals."""
    return _call_with_config(
        RunEarlyWarningScan, llm_config,
        company_metrics=json.dumps(company_metrics, default=str),
        historical_data=json.dumps(historical_data[-12:] if historical_data else [], default=str),
        covenant_data=json.dumps(covenant_data or {}, default=str),
    )


# ============================================================================
# Solution 9: Value-Creation OS
# ============================================================================

def ai_value_creation_plan(
    investment_thesis: str,
    company_data: Dict[str, Any],
    current_initiatives: Optional[List[Dict]] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Create a value-creation plan with 100-day playbooks and OKRs."""
    return _call_with_config(
        GenerateValueCreationPlan, llm_config,
        investment_thesis=investment_thesis,
        company_data=json.dumps(company_data, default=str),
        current_initiatives=json.dumps(current_initiatives or [], default=str),
    )


# ============================================================================
# Solution 10: Cross-Portfolio Procurement
# ============================================================================

def ai_spend_classification(
    spend_data: List[Dict],
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Aggregate spend across portfolio companies to find consolidation opportunities."""
    return _call_with_config(
        AnalyzeSpend, llm_config,
        spend_data=json.dumps(spend_data[:200], default=str),
    )


# ============================================================================
# Solution 11: Revenue & Pricing Optimizer
# ============================================================================

def ai_pricing_optimization(
    customer_data: List[Dict],
    revenue_data: Optional[Dict] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Benchmark pricing, identify revenue leakage, recommend optimization strategies."""
    return _call_with_config(
        RunPricingOptimization, llm_config,
        customer_data=json.dumps(customer_data[:100], default=str),
        revenue_data=json.dumps(revenue_data or {}, default=str),
    )


# ============================================================================
# Solution 12: LP Reporting & DDQ Copilot
# ============================================================================

def ai_lp_report(
    fund_data: Dict[str, Any],
    portfolio_summary: Dict[str, Any],
    period: str = "Q4 2025",
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Generate quarterly LP report and draft DDQ responses."""
    return _call_with_config(
        GenerateLPReport, llm_config,
        fund_data=json.dumps(fund_data, default=str),
        portfolio_summary=json.dumps(portfolio_summary, default=str),
        period=period,
    )


# ============================================================================
# Solution 13: Fund Cash-Flow Forecaster
# ============================================================================

def ai_cashflow_forecast(
    fund_commitments: Dict[str, Any],
    portfolio_data: List[Dict],
    market_assumptions: Optional[Dict] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Model capital calls, distributions, and LP cash-flow exposure."""
    return _call_with_config(
        GenerateCashFlowForecast, llm_config,
        fund_commitments=json.dumps(fund_commitments, default=str),
        portfolio_data=json.dumps(portfolio_data[:50], default=str),
        market_assumptions=json.dumps(market_assumptions or {}, default=str),
    )


# ============================================================================
# Solution 14: Exit-Readiness Platform
# ============================================================================

def ai_exit_readiness(
    company_data: Dict[str, Any],
    financial_metrics: Dict[str, Any],
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Readiness scoring, buyer-matching, and exit-timing analysis."""
    return _call_with_config(
        RunExitReadiness, llm_config,
        company_data=json.dumps(company_data, default=str),
        financial_metrics=json.dumps(financial_metrics, default=str),
    )


# ============================================================================
# Solution 15: Data Quality Platform
# ============================================================================

def ai_data_quality_check(
    data_sources: List[Dict],
    schema_definition: Optional[Dict] = None,
    llm_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Monitor data freshness, completeness, accuracy across portfolio data feeds."""
    return _call_with_config(
        RunDataQualityCheck, llm_config,
        data_sources=json.dumps(data_sources[:50], default=str),
        schema_definition=json.dumps(schema_definition or {}, default=str),
    )
