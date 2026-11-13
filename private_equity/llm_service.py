"""
LLM Service Layer for PE Solutions — DSPy 3.x + Multi-Provider Routing.

No model is hardcoded. Every AI call uses the model the user selected
in the UI (stored as llm_config {provider, model, api_key?, endpoint?}).
The config flows:  Frontend → View → Celery Task → pe_ai_service → here.
"""
import os
import logging
from typing import Optional, Dict, Any

import dspy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider → DSPy LM mapping
# ---------------------------------------------------------------------------

# Env-var defaults (cluster-internal addresses, never exposed to the client)
_LITELLM_URL = os.getenv("LITELLM_URL", "http://aidocumines_litellm:4000/v1")
_LITELLM_KEY = (
    os.getenv("LITELLM_API_KEY")
    or os.getenv("LITELLM_MASTER_KEY")
    or "sk-litellm"
)
_OPENAI_DEFAULT_KEY = os.getenv("OPENAI_API_KEY", "")
_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://aidocumines_ollama:11434")
_VLLM_URL = os.getenv("VLLM_URL", "http://localhost:8000/v1")
_VLLM_KEY = os.getenv("VLLM_API_KEY", "EMPTY")


def _make_litellm_lm(model: str, **overrides) -> dspy.LM:
    """Route through the platform LiteLLM proxy (OpenAI-compatible)."""
    return dspy.LM(
        f"openai/{model}",
        api_base=overrides.get("endpoint") or _LITELLM_URL,
        api_key=_LITELLM_KEY,
        temperature=overrides.get("temperature", 0.3),
        max_tokens=overrides.get("max_tokens", 4096),
    )


def _make_openai_lm(model: str, **overrides) -> dspy.LM:
    """Call OpenAI API directly (when user selected OpenAI provider)."""
    return dspy.LM(
        f"openai/{model}",
        api_key=overrides.get("api_key") or _OPENAI_DEFAULT_KEY,
        temperature=overrides.get("temperature", 0.3),
        max_tokens=overrides.get("max_tokens", 4096),
    )


def _make_ollama_lm(model: str, **overrides) -> dspy.LM:
    """Call a local Ollama instance."""
    return dspy.LM(
        f"ollama/{model}",
        api_base=overrides.get("endpoint") or _OLLAMA_URL,
        temperature=overrides.get("temperature", 0.3),
        max_tokens=overrides.get("max_tokens", 4096),
    )


def _make_vllm_lm(model: str, **overrides) -> dspy.LM:
    """Call a vLLM instance (OpenAI-compatible)."""
    return dspy.LM(
        f"openai/{model}",
        api_base=overrides.get("endpoint") or _VLLM_URL,
        api_key=overrides.get("api_key") or _VLLM_KEY,
        temperature=overrides.get("temperature", 0.3),
        max_tokens=overrides.get("max_tokens", 4096),
    )


_PROVIDER_FACTORIES = {
    "litellm": _make_litellm_lm,
    "openai": _make_openai_lm,
    "ollama": _make_ollama_lm,
    "vllm": _make_vllm_lm,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_lm(llm_config: Optional[Dict[str, Any]] = None, **overrides) -> dspy.LM:
    """
    Create a DSPy LM from the user's llm_config dict.

    Parameters
    ----------
    llm_config : dict, optional
        The config the user selected in the UI.  Keys:
          - provider : str  ("litellm" | "openai" | "ollama" | "vllm")
          - model    : str  (e.g. "gpt-4o", "claude-3-5-sonnet-20241022")
          - api_key  : str  (optional, for openai/vllm direct)
          - endpoint : str  (optional override)
        If None or empty, falls back to the LiteLLM proxy with the
        platform's default model for the client.
    **overrides
        temperature, max_tokens, etc.

    Returns
    -------
    dspy.LM  – a configured LM ready for ChainOfThought / Predict.
    """
    cfg = llm_config or {}
    provider = (cfg.get("provider") or "").lower()
    model = cfg.get("model")

    if not model:
        # Fallback chain: 1) PlatformGlobalSettings 2) LiteLLM proxy query
        pg = _get_platform_defaults()
        if not provider:
            provider = pg.get("provider") or "litellm"
        model = pg.get("model") or ""

    if not model:
        model = _resolve_default_model()
        logger.warning("No model in llm_config or PlatformSettings, falling back to %s", model)

    if not provider:
        provider = "litellm"

    factory = _PROVIDER_FACTORIES.get(provider)
    if factory is None:
        logger.warning("Unknown provider '%s', falling back to litellm", provider)
        factory = _make_litellm_lm

    merged = {**cfg, **overrides}
    # Remove keys that the factory takes as positional args to avoid duplicates
    merged.pop("model", None)
    merged.pop("endpoint", None)
    lm = factory(model, **merged)
    logger.info("Created DSPy LM: provider=%s model=%s", provider, model)
    return lm


def _get_platform_defaults() -> Dict[str, Any]:
    """
    Read platform-global LLM defaults from the database singleton.
    Returns an empty dict on any failure (DB not ready, model not migrated yet, etc.).
    """
    try:
        from system_settings.models import PlatformGlobalSettings
        pg = PlatformGlobalSettings.load()
        return {
            "provider": pg.default_llm_provider or "",
            "model": pg.default_llm_model or "",
            "temperature": pg.default_llm_temperature,
            "max_tokens": pg.default_llm_max_tokens,
        }
    except Exception as e:
        logger.debug("PlatformGlobalSettings not available yet: %s", e)
        return {}


def resolve_model_for_client(client_config: Optional[Dict[str, Any]] = None) -> str:
    """
    Return the model name the user selected (or the platform default).

    This is a convenience helper for callers that only need the model
    string (e.g. logging, token counting) without creating a full LM.
    """
    cfg = client_config or {}
    return cfg.get("model") or _resolve_default_model()


def _resolve_default_model() -> str:
    """
    Query the LiteLLM proxy for the first available model.

    Falls back to "gpt-4o-mini" only if the proxy is unreachable
    (which should never happen in normal operation).
    """
    import urllib.request, urllib.error, json, ssl

    url = f"{_LITELLM_URL}/models"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_LITELLM_KEY}"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            # Filter out wildcard placeholders like "openai/*"
            real = [m["id"] for m in models if isinstance(m, dict) and "*" not in str(m.get("id", ""))]
            if real:
                return real[0]
    except Exception as e:
        logger.warning("Could not query LiteLLM for default model: %s", e)

    return "gpt-4o-mini"
