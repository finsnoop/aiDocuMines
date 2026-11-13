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
    policy, _ = OrganisationPolicy.objects.get_or_create(client_id=client_id)
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


def assistant_policy(user) -> dict:
    policy = policy_for(user)
    if policy is None:
        return {"enabled": True, "web": True, "model": ""}
    return {"enabled": policy.assistant_enabled, "web": policy.assistant_web_enabled,
            "model": policy.default_ai_model or ""}
