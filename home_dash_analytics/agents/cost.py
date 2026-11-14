"""
Cost management agent: tokens -> US dollars, per organisation, per month, against a budget.

* Spend comes from the cost ledger (every AI call on the platform; see agents/usage.py).
* Dollars come from ModelPrice (maintained by platform administrators). Tokens of a model with no
  price are reported as "unpriced", never as free. When a provider reported only a total (no
  prompt/completion split), the cost uses the mean of the two prices and is flagged as estimated.
* Budget states: no_budget, ok, warning (at alert_percent), exhausted. With hard_stop, an exhausted
  budget stops further agent calls for that organisation until the next month or a higher budget.
* State changes are emitted as agent events, once per change.
"""
from __future__ import annotations

from decimal import Decimal

from celery import shared_task
from django.core.cache import cache
from django.db.models import IntegerField, Sum
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from .events import emit

MILLION = Decimal(1_000_000)


def month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def price_for(provider: str, model: str):
    from ..models import ModelPrice
    if not model:
        return None
    candidates = [model, model.split('/', 1)[-1]]      # "openai/gpt-x" via LiteLLM -> "gpt-x"
    for name in candidates:
        price = (ModelPrice.objects.filter(provider=provider or '*', model=name).first()
                 or ModelPrice.objects.filter(provider='*', model=name).first())
        if price:
            return price
    return None


def spend(tenant_id, since=None):
    """Month-to-date usage for one organisation, grouped by provider, model and task."""
    from cost_centre.models import EventLog
    since = since or month_start()
    rows = (EventLog.objects.filter(tenant_id=tenant_id, created_at__gte=since)
            .values('metadata__provider', 'metadata__model', 'metadata__task')
            .annotate(tokens=Coalesce(Sum('tokens_used'), 0),
                      prompt=Coalesce(Sum(Cast(KT('metadata__prompt_tokens'), IntegerField())), 0),
                      completion=Coalesce(Sum(Cast(KT('metadata__completion_tokens'), IntegerField())), 0)))
    total_usd, unpriced, estimated, breakdown = Decimal(0), 0, False, []
    for r in rows:
        provider, model, task = r['metadata__provider'] or '', r['metadata__model'] or '', r['metadata__task'] or ''
        tokens, prompt, completion = int(r['tokens']), int(r['prompt']), int(r['completion'])
        price = price_for(provider, model)
        usd = None
        if price and tokens:
            split = prompt + completion
            usd = (Decimal(prompt) * price.input_usd_per_million + Decimal(completion) * price.output_usd_per_million) / MILLION
            if tokens > split:  # total only: priced at the mean and flagged
                usd += Decimal(tokens - split) * (price.input_usd_per_million + price.output_usd_per_million) / 2 / MILLION
                estimated = True
            total_usd += usd
        elif tokens:
            unpriced += tokens
        breakdown.append({'provider': provider or 'unattributed', 'model': model or 'unattributed',
                          'task': task or 'unattributed', 'tokens': tokens, 'prompt_tokens': prompt,
                          'completion_tokens': completion,
                          'usd': str(usd.quantize(Decimal('0.0001'))) if usd is not None else None})
    breakdown.sort(key=lambda b: -b['tokens'])
    return {'since': since.isoformat(), 'usd': str(total_usd.quantize(Decimal('0.01'))), 'estimated': estimated,
            'tokens': sum(b['tokens'] for b in breakdown), 'unpriced_tokens': unpriced, 'breakdown': breakdown}


def assess(tenant_id, emit_changes=True):
    """Spend plus budget state for one organisation."""
    from ..models import AIBudget
    result = spend(tenant_id)
    budget = AIBudget.objects.filter(tenant_id=tenant_id).first()
    if not budget or not budget.monthly_usd:
        result.update(state='no_budget', budget_usd=None, used_percent=None, hard_stop=False)
    else:
        used = Decimal(result['usd']) / budget.monthly_usd * 100
        state = 'exhausted' if used >= 100 else 'warning' if used >= budget.alert_percent else 'ok'
        result.update(state=state, budget_usd=str(budget.monthly_usd), used_percent=round(float(used), 1),
                      alert_percent=budget.alert_percent, hard_stop=budget.hard_stop)
    if emit_changes:
        key = f'cost-state:{tenant_id}'
        previous = cache.get(key)
        if previous != result['state']:
            cache.set(key, result['state'], None)
            if previous is not None or result['state'] in ('warning', 'exhausted'):
                emit('cost', f"budget.{result['state']}", tenant_id=tenant_id,
                     severity={'exhausted': 'critical', 'warning': 'warning'}.get(result['state'], 'info'),
                     usd=result['usd'], budget_usd=result['budget_usd'], used_percent=result['used_percent'])
    return result


def allow(tenant_id) -> tuple[bool, str]:
    """May agents spend for this organisation right now?"""
    state = assess(tenant_id, emit_changes=False)
    if state['state'] == 'exhausted' and state.get('hard_stop'):
        return False, (f"This month's AI budget (USD {state['budget_usd']}) is used up. "
                       "An administrator can raise it in Settings.")
    return True, ''


@shared_task(name='home_dash_analytics.cost_agent', ignore_result=True, soft_time_limit=120, time_limit=180)
def cost_agent():
    """Periodic review: emits budget warnings/exhaustion for every organisation that spent this month."""
    from cost_centre.models import EventLog
    for tid in EventLog.objects.filter(created_at__gte=month_start()).values_list('tenant_id', flat=True).distinct():
        assess(tid)
