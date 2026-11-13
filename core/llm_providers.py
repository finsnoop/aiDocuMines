"""
Language-model provider configuration shared by the Assistant, workflows and the
model-list proxy.

Server-side configuration (environment variables):

    LITELLM_URL           LiteLLM proxy, e.g. http://aidocumines_litellm:4000/v1
                          (LITELLM_BASE_URL is accepted too)
    LITELLM_API_KEY       LiteLLM virtual key (falls back to LITELLM_MASTER_KEY)
    OLLAMA_URL            e.g. http://ollama:11434
    VLLM_URL              e.g. http://vllm:8000/v1
    VLLM_API_KEY
    OPENAI_API_KEY
    LLM_ALLOWED_HOSTS     comma-separated extra hosts a user may point a provider at
                          (anything else falls back to the server default; this stops
                          the proxy being used to reach arbitrary internal addresses)
    LLM_DEFAULT_PROVIDER  litellm | ollama | vllm | openai   (default litellm)
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

import requests

PROVIDERS = ("litellm", "ollama", "vllm", "openai")
REQUEST_TIMEOUT = 15


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def default_provider() -> str:
    p = _env("LLM_DEFAULT_PROVIDER", "litellm").lower()
    return p if p in PROVIDERS else "litellm"


def default_endpoint(provider: str) -> str:
    return {
        # LITELLM_URL is the name used on the production server (may end in /v1)
        "litellm": _env("LITELLM_BASE_URL") or _env("LITELLM_URL", "http://aidocumines_litellm:4000"),
        "ollama": _env("OLLAMA_URL", "http://ollama:11434"),
        "vllm": _env("VLLM_URL", "http://vllm:8000/v1"),
        "openai": "https://api.openai.com/v1",
    }.get(provider, "")


def default_api_key(provider: str) -> str:
    return {
        "litellm": _env("LITELLM_API_KEY") or _env("LITELLM_MASTER_KEY"),
        "vllm": _env("VLLM_API_KEY", "EMPTY"),
        "openai": _env("OPENAI_API_KEY"),
    }.get(provider, "")


def _allowed_hosts(provider: str) -> set:
    hosts = {h.strip().lower() for h in _env("LLM_ALLOWED_HOSTS").split(",") if h.strip()}
    default_host = urlparse(default_endpoint(provider)).hostname
    if default_host:
        hosts.add(default_host.lower())
    return hosts


def resolve_endpoint(provider: str, requested: Optional[str]) -> str:
    """The requested endpoint if its host is allowed, otherwise the server default."""
    if provider == "openai":
        return default_endpoint("openai")
    if requested:
        parsed = urlparse(requested.strip())
        if parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in _allowed_hosts(provider):
            return requested.strip().rstrip("/")
    return default_endpoint(provider).rstrip("/")


def resolve_api_key(provider: str, requested: Optional[str]) -> str:
    """
    The key to send: the one resolved for this call (an organisation's key, or a personal
    key where the organisation allows them), else the platform's own key. Personal keys
    are dropped before this point unless allowed (grid_documents_interrogation.safety).
    """
    return (requested or "").strip() or default_api_key(provider)


def openai_compatible_base(provider: str, endpoint: str) -> str:
    """LiteLLM and vLLM speak the OpenAI API under /v1."""
    base = endpoint.rstrip("/")
    if provider in ("litellm", "vllm") and not base.endswith("/v1"):
        base += "/v1"
    return base


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def list_models(provider: str, endpoint: Optional[str] = None, api_key: Optional[str] = None) -> dict:
    """
    Models available from a provider, in the provider's own response shape:
    Ollama -> {"models": [{"name": ...}]}, everything else -> {"data": [{"id": ...}]}.
    """
    provider = (provider or default_provider()).lower()
    if provider not in PROVIDERS:
        raise ProviderError(f"Unsupported provider '{provider}'.", status=400)

    url_base = resolve_endpoint(provider, endpoint)
    key = resolve_api_key(provider, api_key)

    if provider == "ollama":
        url = f"{url_base}/api/tags"
        headers = {}
    else:
        if provider == "openai" and not key:
            raise ProviderError("No OpenAI API key is configured.", status=400)
        url = f"{openai_compatible_base(provider, url_base)}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(f"The {provider} model server could not be reached.") from exc
    if resp.status_code in (401, 403):
        raise ProviderError(f"The {provider} model server rejected the API key.", status=502)
    if resp.status_code >= 400:
        raise ProviderError(f"The {provider} model server returned HTTP {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(f"The {provider} model server returned an unreadable response.") from exc

    if provider == "ollama":
        return {"models": data.get("models", []) if isinstance(data, dict) else []}
    items = data.get("data", []) if isinstance(data, dict) else []
    # LiteLLM lists wildcard routes (e.g. "openai/*"); they are not models anyone can pick.
    items = [m for m in items if not (isinstance(m, dict) and "*" in str(m.get("id", "")))]
    return {"object": "list", "data": items}


# ── Choosing a chat model ─────────────────────────────────────────────────────
NOT_CHAT = ("embed", "tts", "whisper", "audio", "realtime", "image", "dall-e", "moderation", "transcribe",
            "search", "instruct", "babbage", "davinci", "codex", "computer-use", "sora")
PREFERRED = ("gpt-4o-mini", "gpt-4.1-mini", "gpt-4o", "gpt-4.1")


def pick_chat_model(ids, preferred=None):
    """
    A model that answers chat completions: `preferred` if offered, else a sensible current
    default. Gateways such as LiteLLM name models "<provider>/<model>" (openai/gpt-4o-mini),
    so names are matched with or without that prefix.
    """
    chat = [i for i in ids if i and "*" not in i and not any(w in i.lower() for w in NOT_CHAT)]
    bare = {i.split("/", 1)[-1]: i for i in chat}
    for want in (preferred, *PREFERRED):
        if not want:
            continue
        if want in chat:
            return want
        hit = bare.get(want.split("/", 1)[-1])
        if hit:
            return hit
    return chat[0] if chat else None


def model_for(provider: str, requested: str) -> str:
    """The requested model as this provider names it (adds or drops a gateway prefix if needed)."""
    requested = (requested or "").strip()
    if not requested:
        return available_chat_model(provider) or ""
    if provider != "litellm":
        return requested.split("/", 1)[-1] if provider == "openai" and requested.startswith("openai/") else requested
    try:
        from django.core.cache import cache
        ids = cache.get(f"llm:ids:{provider}")
        if ids is None:
            data = list_models(provider)
            ids = [m.get("id") for m in data.get("data", [])]
            cache.set(f"llm:ids:{provider}", ids, 3600)
    except Exception:
        return requested
    if requested in ids:
        return requested
    return pick_chat_model(ids, requested) if any(i.split("/", 1)[-1] == requested.split("/", 1)[-1] for i in ids) else requested


def available_chat_model(provider: str):
    """A working chat model for the provider from its live model list (cached for an hour)."""
    from django.core.cache import cache

    key = f"llm:chat-model:{provider}"
    try:
        cached = cache.get(key)
        if cached:
            return cached
    except Exception:
        cached = None
    try:
        data = list_models(provider)
        ids = [m.get("name") or m.get("model") for m in data.get("models", [])] if provider == "ollama" \
            else [m.get("id") for m in data.get("data", [])]
        model = pick_chat_model(ids)
    except ProviderError:
        model = None
    if model:
        try:
            cache.set(key, model, 3600)
        except Exception:
            pass
    return model


def is_missing_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("deprecated", "does not exist", "model_not_found", "not found", "404", "unknown model"))
