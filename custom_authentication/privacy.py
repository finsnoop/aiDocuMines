"""
Data-subject rights for the people who use the platform (POPIA ss 23-25, GDPR arts 15, 17, 20), and the platform's
retention defaults. Requests about people named *inside* documents are handled by the organisation with the
regulatory-compliance tools (DSAR requests, search, redaction, anonymisation).

    GET  /api/v1/auth/me/personal-data/                     your own data (JSON download)
    GET  /api/v1/auth/admin/people/<id>/personal-data/      an administrator exports a person's data
    POST /api/v1/auth/admin/people/<id>/erase/              {confirm_email, transfer_to?} erase a person's data

Erasure keeps the organisation's records and the evidence the law requires, and removes the rest:
* removed: name, contact details, address, preferences, sign-in secrets (password, 2FA secret, API keys, tokens and
  codes), assistant questions and answers, activity details (addresses, query previews);
* pseudonymised: the account itself (email and username become erased-<id>@erased.invalid), so documents,
  versions and workflow history still point at "a former user" instead of breaking;
* kept: documents (they belong to the organisation, which is the responsible party; optionally transferred to a
  colleague), the tamper-evident administrative audit trail and AI usage/billing records, under the exemptions
  for legal obligations and legal claims (POPIA s 14(1), GDPR art 17(3)(b) and (e)).
Documents under legal hold stay where they are. The erasure itself is recorded in the audit trail without the
person's email.

Retention defaults (apply_retention, daily; override with the environment variables named in RETENTION):
see RETENTION below and docs/PRIVACY_AND_DATA_PROTECTION.md.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
ERASED_DOMAIN = "erased.invalid"

# name -> (environment variable, default days, what it covers)
RETENTION = {
    "activity_logs": ("RETENTION_ACTIVITY_DAYS", 730, "sign-in and activity records (UserActivityLog)"),
    "assistant_queries": ("RETENTION_ASSISTANT_DAYS", 730, "assistant and grid questions and answers"),
    "harness_runs": ("RETENTION_HARNESS_RUNS_DAYS", 400, "AI harness monitoring records (no question or answer text)"),
}


def retention_days(name) -> int:
    env, default, _ = RETENTION[name]
    try:
        value = int(os.getenv(env, default))
    except ValueError:
        value = default
    return max(30, value)          # never shorter than 30 days: security investigations need recent history


# ── export ──────────────────────────────────────────────────────────────────────────────────
def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _rows(qs, fields, limit=50_000):
    return [{f: _iso(getattr(row, f, None)) for f in fields} for row in qs[:limit]]


def personal_data(user) -> dict:
    """Everything the platform holds about this person as a user, in a portable, machine-readable form.
    Document *contents* belong to the organisation and are listed, not included."""
    from core.models import File, UserPreference
    from .models import AccountDeletionRequest, APIKey, UserActivityLog
    profile_fields = ["email", "username", "first_name", "last_name", "contact_name", "contact_phone", "contact_email",
                      "address", "industry", "use_case", "date_joined", "profile_created_at", "last_login", "last_activity",
                      "is_active", "is_2fa_enabled", "two_factor_enabled", "notifications_enabled", "preferences",
                      "user_preferences", "subscription_plan"]
    data = {
        "format": "aiDocuMines personal data export v1",
        "generated_at": timezone.now().isoformat(),
        "profile": {f: _iso(getattr(user, f, None)) for f in profile_fields},
        "organisation": getattr(user.client, "name", None),
        "roles": sorted(user.groups.values_list("name", flat=True)),
        "interface_preferences": _rows(UserPreference.objects.filter(user=user), ["sidebar_pinned", "updated_at"]),
        "api_keys": _rows(APIKey.objects.filter(user=user), ["client_id", "created_at"]),      # secrets never exported
        "activity": _rows(UserActivityLog.objects.filter(user=user).order_by("-timestamp"), ["event", "timestamp", "metadata"]),
        "deletion_request": _rows(AccountDeletionRequest.objects.filter(user=user), ["requested_at", "is_processed"]),
        "documents_owned": _rows(File.objects.filter(user=user).order_by("-id"),
                                 [f for f in ("id", "filename", "project_id", "service_id", "created_at", "file_size")
                                  if _has(File, f)]),
    }
    try:
        from grid_documents_interrogation.models import Query
        data["assistant_questions"] = _rows(Query.objects.filter(user=user).order_by("-created_at"),
                                            ["query_text", "response_text", "created_at"])
    except Exception:
        logger.debug("Assistant questions unavailable for export", exc_info=True)
    from core.models import AssistantTurn
    data["assistant_conversation_questions"] = [
        {"object": t.conversation.label, "question": t.content, "asked_at": t.created_at.isoformat()}
        for t in AssistantTurn.objects.filter(conversation__user=user, role="user").select_related("conversation")
        .order_by("-created_at")[:5000]]
    try:
        from cost_centre.models import TokenUsage
        data["ai_usage"] = _rows(TokenUsage.objects.filter(user=user).order_by("-created_at"),
                                 [f for f in ("created_at", "tokens_used", "cost", "model") if _has(TokenUsage, f)])
    except Exception:
        logger.debug("AI usage unavailable for export", exc_info=True)
    return data


def _has(model, field) -> bool:
    try:
        model._meta.get_field(field)
        return True
    except Exception:
        return False


# ── erasure ─────────────────────────────────────────────────────────────────────────────────
class ErasureRefused(Exception):
    """A reason the erasure cannot go ahead, safe to show to the administrator."""


def check_erasable(actor, target):
    from core.access import is_tenant_admin
    if target.pk == actor.pk:
        raise ErasureRefused("You cannot erase your own account. Ask another administrator.")
    if target.is_superuser:
        raise ErasureRefused("Platform administrator accounts cannot be erased here.")
    if target.email.endswith("@" + ERASED_DOMAIN):
        raise ErasureRefused("This person's data has already been erased.")
    if is_tenant_admin(target) and target.client_id:
        from .models import CustomUser
        others = CustomUser.objects.filter(client_id=target.client_id, is_active=True, groups__name="Admin") \
            .exclude(pk=target.pk).exists()
        if not others:
            raise ErasureRefused("This is the organisation's last active administrator. Make someone else an "
                                 "administrator first.")


@transaction.atomic
def erase(actor, target, transfer_to=None) -> dict:
    """Erase a person's personal data (see the module docstring). Returns a summary of what was done."""
    from oauth2_provider.models import AccessToken, Grant, RefreshToken as OAuthRefreshToken
    from core.models import File, UserPreference
    from .models import (AccountDeletionRequest, APIKey, CustomUser, PasswordResetToken, RefreshToken,
                         TwoFactorCode, UserActivityLog)
    check_erasable(actor, target)
    target = CustomUser.objects.select_for_update().get(pk=target.pk)
    summary = {}

    if transfer_to is not None:
        if transfer_to.client_id != target.client_id or not transfer_to.is_active or transfer_to.pk == target.pk:
            raise ErasureRefused("Documents can only be transferred to an active colleague in the same organisation.")
        from core.holds import is_held
        movable = [f.pk for f in File.objects.filter(user=target) if not is_held(f)]
        summary["documents_transferred"] = File.objects.filter(pk__in=movable).update(user=transfer_to)
        summary["documents_kept_on_hold"] = File.objects.filter(user=target).count()
    else:
        summary["documents_kept"] = File.objects.filter(user=target).count()

    # Sign-in secrets and sessions.
    summary["sessions_ended"] = (AccessToken.objects.filter(user=target).delete()[0]
                                 + OAuthRefreshToken.objects.filter(user=target).delete()[0]
                                 + Grant.objects.filter(user=target).delete()[0]
                                 + RefreshToken.objects.filter(user=target).delete()[0])
    PasswordResetToken.objects.filter(user=target).delete()
    TwoFactorCode.objects.filter(user=target).delete()
    summary["api_keys_removed"] = APIKey.objects.filter(user=target).delete()[0]
    UserPreference.objects.filter(user=target).delete()

    # Content the person wrote, and activity details.
    try:
        from grid_documents_interrogation.models import Query
        summary["assistant_questions_removed"] = Query.objects.filter(user=target).delete()[0]
    except Exception:
        logger.debug("Assistant questions not removed", exc_info=True)
    summary["chat_activity_removed"] = UserActivityLog.objects.filter(user=target, event="CHAT_QUERY").delete()[0]
    from core.models import AssistantConversation
    summary["assistant_conversations_removed"] = AssistantConversation.objects.filter(user=target).delete()[0]
    UserActivityLog.objects.filter(user=target).update(metadata=None)
    AccountDeletionRequest.objects.filter(user=target).update(is_processed=True)

    # The account: pseudonymous and unusable.
    alias = f"erased-{target.pk}@{ERASED_DOMAIN}"
    for field in ("first_name", "last_name", "contact_name", "contact_phone", "contact_email", "address",
                  "industry", "use_case", "temp_password", "client_secret_plain"):
        if hasattr(target, field):
            setattr(target, field, "" if CustomUser._meta.get_field(field).null is False else None)
    target.email, target.username = alias, f"erased-{target.pk}"
    target.preferences, target.user_preferences = {}, None
    target.totp_secret, target.is_2fa_enabled, target.is_2fa_verified, target.two_factor_enabled = None, False, False, False
    target.is_active, target.account_status = False, "inactive"
    target.set_unusable_password()
    target.save()
    target.groups.clear()
    target.roles.clear()
    summary["account"] = "pseudonymised and disabled"
    logger.info("Personal data erased for user %s by %s", target.pk, actor.pk)
    return summary


# ── retention ───────────────────────────────────────────────────────────────────────────────
def apply_retention(now=None) -> dict:
    """Delete or minimise records older than their retention period. Audit events, documents and billing
    amounts are never touched here (documents follow the organisation's retention schedules)."""
    from .models import UserActivityLog
    now = now or timezone.now()
    out = {}
    out["activity_logs"] = UserActivityLog.objects.filter(
        timestamp__lt=now - timedelta(days=retention_days("activity_logs"))).delete()[0]
    try:
        from grid_documents_interrogation.models import Query
        out["assistant_queries"] = Query.objects.filter(
            created_at__lt=now - timedelta(days=retention_days("assistant_queries"))).delete()[0]
    except Exception:
        logger.debug("Assistant query retention skipped", exc_info=True)
    from core.assistant_memory import purge_expired
    from core.models import HarnessRun
    out["assistant_conversations"] = purge_expired(now)      # each organisation's memory period
    out["harness_runs"] = HarnessRun.objects.filter(
        created_at__lt=now - timedelta(days=retention_days("harness_runs"))).delete()[0]
    logger.info("Retention applied: %s", out)
    return out


# ── data residency statement (per organisation) ─────────────────────────────────────────────
MODEL_VENDORS = (("gpt", "OpenAI"), ("o1", "OpenAI"), ("o3", "OpenAI"), ("o4", "OpenAI"), ("claude", "Anthropic"),
                 ("gemini", "Google"), ("mistral", "Mistral AI"), ("llama", "Meta model (host depends on route)"),
                 ("deepseek", "DeepSeek"), ("openrouter/", "OpenRouter"), ("azure/", "Microsoft Azure OpenAI"))
LOCAL_PROVIDERS = {"ollama", "vllm"}
CONNECTOR_NAMES = {"google_drive": "Google Drive", "dropbox": "Dropbox", "onedrive": "Microsoft OneDrive",
                   "teams": "Microsoft Teams", "box": "Box", "slack": "Slack", "notion": "Notion",
                   "salesforce": "Salesforce", "hubspot": "HubSpot", "zapier": "Zapier", "make": "Make"}


def _vendor(model: str) -> str:
    m = (model or "").lower()
    return next((v for prefix, v in MODEL_VENDORS if m.startswith(prefix) or f"/{prefix}" in m), "the model's provider")


def residency_statement(client) -> dict:
    """Where this organisation's data is stored and which services process it, from live configuration."""
    from core.llm_providers import default_provider
    from .models import OrganisationPolicy
    policy = OrganisationPolicy.objects.filter(client=client).first()
    env = os.getenv
    provider = (getattr(policy, "ai_provider", "") or default_provider() or "").lower()
    models = [m.get("model") for m in (getattr(policy, "allowed_ai_models", None) or []) if isinstance(m, dict) and m.get("model")]
    default_model = getattr(policy, "default_ai_model", "") or env("ASSISTANT_MODEL") or env("OPENAI_MODEL") or ""
    if default_model and default_model not in models:
        models.insert(0, default_model)
    if provider in LOCAL_PROVIDERS:
        ai = {"provider": provider, "location": "On the platform's own servers (no third-party AI provider)", "vendors": []}
    else:
        vendors = sorted({_vendor(m) for m in models}) or ["the configured model provider"]
        ai = {"provider": provider or "litellm",
              "location": "Sent to the AI provider for each request; prompts are not used for training under the "
                          "providers' API terms. The provider's processing region applies.",
              "vendors": vendors, "models": models}
    services = []
    try:
        from contracts.models import SignatureRequest
        for p in SignatureRequest.objects.filter(client=client).values_list("provider", flat=True).distinct():
            services.append({"service": {"docusign": "DocuSign", "adobe": "Adobe Acrobat Sign"}.get(p, p),
                             "purpose": "Electronic signature of documents you send for signing"})
    except Exception:
        logger.debug("E-signature providers unavailable", exc_info=True)
    try:
        from integrations.models import ConnectorAccount
        for s in ConnectorAccount.objects.filter(user__client=client).values_list("service", flat=True).distinct():
            services.append({"service": CONNECTOR_NAMES.get(s, s.replace("_", " ").title()),
                             "purpose": "Connector your people chose to link (documents they import or export)"})
    except Exception:
        logger.debug("Connectors unavailable", exc_info=True)
    if env("EMAIL_HOST"):
        services.append({"service": f"Email delivery ({env('EMAIL_HOST')})", "purpose": "Sign-in codes, notifications"})
    if getattr(policy, "assistant_web_enabled", False):
        services.append({"service": "Web search provider", "purpose": "Only the question text, when someone asks the "
                                                                      "assistant to search the web"})
    return {
        "organisation": client.name,
        # Empty when the platform team has not recorded it yet; `missing` names the server settings to fill in.
        "storage": {"provider": env("DATA_HOSTING_PROVIDER") or "",
                    "region": env("DATA_HOSTING_REGION") or "",
                    "backups": env("DATA_BACKUP_REGION") or env("DATA_HOSTING_REGION") or "",
                    "encryption": "TLS in transit; database fields holding secrets are encrypted with the platform key; "
                                  "disk encryption depends on the hosting provider (see the data protection document)"},
        "ai_processing": ai,
        "missing": [name for name in ("DATA_HOSTING_PROVIDER", "DATA_HOSTING_REGION", "DATA_BACKUP_REGION")
                    if not env(name) and not (name == "DATA_BACKUP_REGION" and env("DATA_HOSTING_REGION"))],
        "other_services": services,
        "generated_at": timezone.now().isoformat(),
    }
