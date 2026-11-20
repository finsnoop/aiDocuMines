"""
Token prices for turning AI usage into US dollars.

Where a price comes from, in order:
1. ModelPrice: a price a platform administrator set (always wins).
2. LiteLLM's community-maintained price registry (input and output price per token for ~7,000
   models across OpenAI, Anthropic, Google, DeepSeek, Mistral, Azure, Bedrock, ...). This is the
   same data litellm.cost_per_token() uses. A trimmed snapshot ships with the code
   (data/litellm_prices.json) and a daily task refreshes it from LiteLLM.
3. Aliases configured in the platform's LiteLLM proxy are resolved to the model behind them.
4. Self-hosted models (Ollama, vLLM) have no per-token charge: priced at 0.

A model that matches none of these is reported as unpriced, never as free.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from celery import shared_task
from django.core.cache import cache

logger = logging.getLogger(__name__)

REGISTRY_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "litellm_prices.json"
CACHE_KEY = "ai-prices:litellm"
ALIAS_KEY = "ai-prices:proxy-aliases"
SELF_HOSTED = {"ollama", "vllm"}
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    source: str


def trim(raw: dict) -> dict:
    """{model: [input USD per million, output USD per million, provider]} for priced entries."""
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or name == "sample_spec":
            continue
        cin = spec.get("input_cost_per_token")
        if not isinstance(cin, (int, float)):
            continue
        cout = spec.get("output_cost_per_token")
        out[name] = [round(cin * 1_000_000, 6), round((cout if isinstance(cout, (int, float)) else 0) * 1_000_000, 6),
                     str(spec.get("litellm_provider") or "")]
    return out


@lru_cache(maxsize=1)
def _snapshot() -> dict:
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Bundled AI price snapshot missing or unreadable: %s", SNAPSHOT)
        return {}


def registry() -> dict:
    """The latest refreshed registry, or the bundled snapshot."""
    try:
        latest = cache.get(CACHE_KEY)
    except Exception:
        latest = None
    return latest or _snapshot()


def refresh(timeout=30) -> int:
    """Download LiteLLM's registry, keep the prices, and use them from now on. Returns the model count."""
    import requests
    response = requests.get(REGISTRY_URL, timeout=timeout, stream=True)
    response.raise_for_status()
    body = response.raw.read(30 * 1024 * 1024 + 1, decode_content=True)
    if len(body) > 30 * 1024 * 1024:
        raise ValueError("The price registry is unexpectedly large.")
    prices = trim(json.loads(body))
    if len(prices) < 500:
        raise ValueError("The price registry looks incomplete; keeping the current prices.")
    cache.set(CACHE_KEY, prices, None)
    return len(prices)


@shared_task(name="home_dash_analytics.refresh_model_prices", ignore_result=True, soft_time_limit=90, time_limit=120)
def refresh_model_prices():
    try:
        logger.info("AI price registry refreshed: %d models", refresh())
    except Exception:
        logger.warning("AI price registry refresh failed; the previous prices stay in use.", exc_info=True)


def proxy_aliases() -> dict:
    """{alias: model behind it} from the platform's LiteLLM proxy (cached; empty when unreachable)."""
    aliases = cache.get(ALIAS_KEY)
    if aliases is not None:
        return aliases
    aliases = {}
    try:
        import requests
        from core.llm_providers import default_api_key, default_endpoint
        base = default_endpoint("litellm").rstrip("/")
        base = base[:-3] if base.endswith("/v1") else base
        key = default_api_key("litellm")
        response = requests.get(f"{base}/model/info", timeout=3,
                                headers={"Authorization": f"Bearer {key}"} if key else {})
        if response.ok:
            for entry in response.json().get("data", []):
                alias = entry.get("model_name")
                behind = (entry.get("litellm_params") or {}).get("model")
                if alias and behind:
                    aliases[str(alias)] = str(behind)
        cache.set(ALIAS_KEY, aliases, 3600)
    except Exception:
        cache.set(ALIAS_KEY, aliases, 300)
    return aliases


def _candidates(provider: str, model: str):
    names = [model]
    if "/" in model:
        names.append(model.split("/", 1)[1])
    if provider and "/" not in model:
        names.append(f"{provider}/{model}")
    return names


def lookup(provider: str, model: str):
    """A Price for a model from the registry (or self-hosted = 0), or None when unknown."""
    if not model:
        return None
    provider = (provider or "").lower()
    prices = registry()
    names = _candidates(provider, model)
    behind = proxy_aliases().get(model) if provider in ("litellm", "openai-compatible", "") else None
    if behind:
        names = _candidates("", behind) + names
    for name in names:
        hit = prices.get(name)
        if hit:
            return Price(Decimal(str(hit[0])), Decimal(str(hit[1])), f"LiteLLM registry: {name}")
    target = (behind or model).lower()
    if provider in SELF_HOSTED or target.split("/", 1)[0] in SELF_HOSTED:
        return Price(Decimal(0), Decimal(0), "self-hosted model (no per-token charge)")
    return None
