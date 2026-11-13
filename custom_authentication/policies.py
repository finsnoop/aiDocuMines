"""Organisation policies (see OrganisationPolicy) and the checks that enforce them."""
from __future__ import annotations

import os

from django.core.cache import cache

POLICY_CACHE_SECONDS = 60


def policy_for(user):
    """The caller's organisation policy (cached briefly); None for users without an organisation."""
    from custom_authentication.models import OrganisationPolicy

    client_id = getattr(user, "client_id", None)
    if not client_id:
        return None
    key = f"org-policy:{client_id}"
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached
    except Exception:
        cached = None
    # The organisation's AI key is never part of the cached copy (it would sit decrypted in Redis).
    policy, _ = OrganisationPolicy.objects.defer("ai_api_key").get_or_create(client_id=client_id)
    try:
        cache.set(key, policy, POLICY_CACHE_SECONDS)
    except Exception:
        pass
    return policy


def forget_policy(client_id) -> None:
    try:
        cache.delete(f"org-policy:{client_id}")
    except Exception:
        pass


def upload_violation(user, filename: str, size_bytes: int) -> str | None:
    """Reason the organisation does not allow this upload, or None."""
    policy = policy_for(user)
    if policy is None:
        return None
    ext = (os.path.splitext(filename or "")[1] or "").lstrip(".").lower()
    allowed = {e.strip().lstrip(".").lower() for e in (policy.allowed_file_types or "").split(",") if e.strip()}
    if allowed and ext not in allowed:
        return f"Your organisation does not allow .{ext or '?'} files. Allowed: {', '.join(sorted(allowed))}."
    if policy.max_upload_mb and size_bytes > policy.max_upload_mb * 1024 * 1024:
        return f"File is larger than your organisation's limit of {policy.max_upload_mb} MB."
    return None


def ai_access(user) -> dict:
    """
    How the user's AI calls are made: {"provider", "has_org_key", "allow_personal", "org_id"}.
    The key itself is never returned here; organisation_ai_key() reads it only when calling a model.
    """
    policy = policy_for(user)
    if policy is None:
        return {"provider": "", "has_org_key": False, "allow_personal": True, "org_id": None}
    from custom_authentication.models import OrganisationPolicy
    # The column is encrypted (even an empty value), so check the decrypted value.
    row = OrganisationPolicy.objects.filter(pk=policy.pk).only("ai_api_key").first() if policy.pk else None
    has_key = bool(row and row.ai_api_key)
    return {"provider": policy.ai_provider or "", "has_org_key": has_key,
            "allow_personal": bool(policy.allow_personal_ai_keys), "org_id": policy.client_id}


def organisation_ai_key(client_id) -> str:
    """The organisation's AI API key (decrypted), read straight from the database."""
    from custom_authentication.models import OrganisationPolicy
    if not client_id:
        return ""
    row = OrganisationPolicy.objects.filter(client_id=client_id).only("ai_api_key").first()
    return (row.ai_api_key or "") if row else ""


def assistant_policy(user) -> dict:
    policy = policy_for(user)
    if policy is None:
        return {"enabled": True, "web": True, "model": ""}
    return {"enabled": policy.assistant_enabled, "web": policy.assistant_web_enabled,
            "model": policy.default_ai_model or ""}
