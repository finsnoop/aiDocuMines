# home_dash_analytics/utils.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Iterable

# Core Django ORM imports
from django.db.models import (
    Count, Sum, Avg, Max, Q, Value,
    BigIntegerField, IntegerField, FloatField,
)
from django.db.models.functions import TruncMonth, Coalesce
from django.utils.dateparse import parse_datetime

from django.db.models.functions import TruncDay, ExtractHour

# OAuth2: map client_id -> user
from oauth2_provider.models import Application, AccessToken, RefreshToken, Grant, IDToken

# ── Imports from your apps (per MODEL_INFO_FULL.txt) ──────────────────────────
from custom_authentication.models import CustomUser, APIKey, UserActivityLog, UserAPICall
from core.models import File, Run, Storage, EndpointResponseTable, Metadata, Webhook
from document_operations.models import (
    FileAccessEntry, FileAuditLog, FileFolderLink, FileVersion, EffectiveAccess
)
from file_monitor.models import FileEventLog
from document_search.models import SearchQueryLog, VectorChunk
from document_structures.models import (
    DocumentStructureRun, DocumentElement, DocumentTable, SectionEdit
)
from document_ocr.models import OCRFile
from document_translation.models import TranslationFile
from integrations.models import IntegrationLog
from cost_centre.models import TokenUsage, Budget, Subscription, PaymentHistory, EventLog
from grid_documents_interrogation.models import Topic, Query, DatabaseConnection
from platform_data_insights.models import UserInsights
from system_settings.models import SystemSettings

from django.contrib.admin.models import LogEntry as AdminLogEntry
from core.access import accessible_files, tenant_id


# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_WINDOW_DAYS = 30


# ── Security helper ───────────────────────────────────────────────────────────
# ── Time helpers ──────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_since(since: Optional[str]) -> Optional[datetime]:
    if not since:
        return None
    dt = parse_datetime(since)
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Typed aggregators (avoid “mixed types” errors) ────────────────────────────
def _sum_bigint(qs, field: str) -> int:
    """Sum a field with BigInteger 0 sentinel (for PositiveInteger/BigInteger)."""
    return int(
        qs.aggregate(total=Coalesce(Sum(field, output_field=BigIntegerField()),
                                    Value(0, output_field=BigIntegerField())))["total"] or 0
    )

def _sum_int(qs, field: str) -> int:
    return int(
        qs.aggregate(total=Coalesce(Sum(field, output_field=IntegerField()),
                                    Value(0, output_field=IntegerField())))["total"] or 0
    )

def _avg_float(qs, field: str) -> float:
    return float(
        qs.aggregate(avg=Coalesce(Avg(field, output_field=FloatField()),
                                  Value(0.0, output_field=FloatField())))["avg"] or 0.0
    )

def _max(qs, field: str):
    return qs.aggregate(m=Max(field))["m"]

def _distinct_count(qs, field: str) -> int:
    return qs.values(field).distinct().count()


def _content_type_map_for_user_logs(user_id: int) -> list[dict]:
    qs = AdminLogEntry.objects.filter(user_id=user_id)
    rows = (
        qs.values("content_type__app_label", "content_type__model")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [
        {"app": r["content_type__app_label"], "model": r["content_type__model"], "count": r["count"]}
        for r in rows
    ]


# ── Section builders (each returns a small dict) ──────────────────────────────
def section_user(*, user: CustomUser, since: Optional[str] = None, **_):
    return {
        "id": user.id,
        "email": user.email,
        "date_joined": user.date_joined,
        "last_login": user.last_login,
        "last_activity": getattr(user, "last_activity", None),
        "subscription_plan": getattr(user, "subscription_plan", None),
        "plan_expiry_date": getattr(user, "plan_expiry_date", None),
        "total_api_calls_made": getattr(user, "total_api_calls_made", 0),
        "two_factor": {
            "enabled": getattr(user, "is_2fa_enabled", False) or getattr(user, "two_factor_enabled", False),
            "verified": getattr(user, "is_2fa_verified", False),
        },
    }


def _file_base_qs(user: CustomUser, project_id: Optional[str], service_id: Optional[str]):
    qs = accessible_files(user, 'view', File.objects.filter(user__client_id=tenant_id(user)))
    if project_id:
        qs = qs.filter(project_id=project_id)
    if service_id:
        qs = qs.filter(service_id=service_id)
    return qs


def section_files(*, user: CustomUser, project_id: Optional[str] = None, service_id: Optional[str] = None,
                  since: Optional[str] = None, **_):
    files_qs = _file_base_qs(user, project_id, service_id)
    since_dt = _parse_since(since)
    return {
        "total": files_qs.count(),
        "new_in_window": files_qs.filter(created_at__gte=since_dt).count() if since_dt else None,
        "storage_bytes": _sum_bigint(files_qs, "file_size"),
        "versions_total": FileVersion.objects.filter(file__in=files_qs).count(),
        "doc_type_distribution": list(
            files_qs.values("document_type").annotate(count=Count("id")).order_by("-count")[:15]
        ),
        "extension_distribution": list(
            files_qs.values("extension").annotate(count=Count("id")).order_by("-count")[:15]
        ),
    }


def section_runs(*, user: CustomUser, since: Optional[str] = None, **_):
    qs = Run.objects.filter(user_id=user.id)
    since_dt = _parse_since(since)
    if since_dt:
        qs = qs.filter(created_at__gte=since_dt)
    return {
        "total": qs.count(),
        "by_status": list(qs.values("status").annotate(count=Count("run_id")).order_by("-count")),
    }


def section_storage(*, user: CustomUser, since: Optional[str] = None, **_):
    storages_qs = Storage.objects.filter(user_id=user.id)
    runs_qs = Run.objects.filter(user_id=user.id)
    endpoints_qs = EndpointResponseTable.objects.filter(run__user_id=user.id)
    return {
        "storages_total": storages_qs.count(),
        "runs_total": runs_qs.count(),
        "endpoint_events_total": endpoints_qs.count(),
    }


def section_search(*, user: CustomUser, since: Optional[str] = None, **_):
    qs = SearchQueryLog.objects.filter(user_id=user.id)
    since_dt = _parse_since(since)
    return {
        "total": qs.count(),
        "in_window": qs.filter(created_at__gte=since_dt).count() if since_dt else None,
        "avg_duration_ms": round(_avg_float(qs, "duration_ms"), 2),
        "last_search_at": _max(qs, "created_at"),
        "vector_chunks_total": VectorChunk.objects.filter(user_id=user.id).count(),
    }


def section_ocr(*, user: CustomUser, since: Optional[str] = None, project_id: Optional[str] = None,
                service_id: Optional[str] = None, **_):
    qs = OCRFile.objects.filter(original_file__user_id=user.id)
    if project_id:
        qs = qs.filter(original_file__project_id=project_id)
    if service_id:
        qs = qs.filter(original_file__service_id=service_id)
    return {
        "files_total": qs.count(),
        "runs_distinct": _distinct_count(qs, "run"),
    }


def section_translation(*, user: CustomUser, since: Optional[str] = None, project_id: Optional[str] = None,
                        service_id: Optional[str] = None, **_):
    qs = TranslationFile.objects.filter(original_file__user_id=user.id)
    if project_id:
        qs = qs.filter(original_file__project_id=project_id)
    if service_id:
        qs = qs.filter(original_file__service_id=service_id)
    return {
        "files_total": qs.count(),
        "runs_distinct": _distinct_count(qs, "run"),
    }


def section_operations(*, user: CustomUser, since: Optional[str] = None, **_):
    since_dt = _parse_since(since)

    # File audit logs: actor or owner
    audit_qs = FileAuditLog.objects.filter(Q(user_id=user.id) | Q(file__user_id=user.id))
    if since_dt:
        audit_qs = audit_qs.filter(timestamp__gte=since_dt)

    # File events: triggered_by user or events on user's files
    event_qs = FileEventLog.objects.filter(Q(triggered_by_id=user.id) | Q(file__user_id=user.id))
    if since_dt:
        event_qs = event_qs.filter(timestamp__gte=since_dt)

    return {
        "file_audit_breakdown": list(
            audit_qs.values("action").annotate(count=Count("id")).order_by("-count")
        ),
        "file_event_breakdown": list(
            event_qs.values("event_type").annotate(count=Count("id")).order_by("-count")
        ),
    }


def section_billing(*, user: CustomUser, since: Optional[str] = None, **_):
    since_dt = _parse_since(since)

    tokens_qs = TokenUsage.objects.filter(user_id=user.id)
    if since_dt:
        tokens_qs = tokens_qs.filter(created_at__gte=since_dt)

    tokens_total = _sum_bigint(tokens_qs, "tokens_used")

    tokens_6mo = TokenUsage.objects.filter(
        user_id=user.id, created_at__gte=_utcnow() - timedelta(days=180)
    )
    tokens_by_month = list(
        tokens_6mo
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(
            tokens=Coalesce(
                Sum("tokens_used", output_field=BigIntegerField()),
                Value(0, output_field=BigIntegerField()),
            )
        )
        .order_by("month")
    )

    budget = Budget.objects.filter(user_id=user.id).order_by("-created_at").first()
    sub = Subscription.objects.filter(user_id=user.id).order_by("-created_at").first()

    recent_payments = list(
        PaymentHistory.objects.filter(user_id=user.id)
        .order_by("-payment_date")
        .values("amount_paid", "currency", "payment_date", "payment_method")[:5]
    )

    cc_events = list(
        EventLog.objects.filter(user_id=user.id)
        .values("event_type", "service_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    return {
        "tokens_total": tokens_total,
        "tokens_by_month": tokens_by_month,
        "budget": {
            "token_limit": getattr(budget, "token_limit", None) if budget else None,
            "financial_limit": getattr(budget, "financial_limit", None) if budget else None,
        },
        "subscription": {
            "plan_code": getattr(sub, "plan_code", None) if sub else None,
            "stripe_status": getattr(sub, "stripe_status", None) if sub else None,
            "billing_cycle_start": getattr(sub, "billing_cycle_start", None) if sub else None,
            "billing_cycle_end": getattr(sub, "billing_cycle_end", None) if sub else None,
            "amount_billed": getattr(sub, "amount_billed", None) if sub else None,
            "seat_count": getattr(sub, "seat_count", None) if sub else None,
        },
        "recent_payments": recent_payments,
        "event_breakdown": cc_events,
    }


def section_integrations(*, user: CustomUser, since: Optional[str] = None, **_):
    qs = IntegrationLog.objects.filter(user_id=user.id)
    since_dt = _parse_since(since)
    if since_dt:
        qs = qs.filter(timestamp__gte=since_dt)
    return {
        "by_connector": list(
            qs.values("connector").annotate(count=Count("id")).order_by("-count")
        )
    }


def section_security(*, user: CustomUser, **_):
    # Use relations off user to avoid custom reverse names
    groups_count = user.groups.count()
    permissions_count = user.user_permissions.count()
    api_keys_count = APIKey.objects.filter(user_id=user.id).count()

    oauth_apps_count = Application.objects.filter(user_id=user.id).count()
    oauth_access_tokens = AccessToken.objects.filter(user_id=user.id).count()
    oauth_refresh_tokens = RefreshToken.objects.filter(user_id=user.id).count()
    oauth_grants = Grant.objects.filter(user_id=user.id).count()
    oauth_id_tokens = IDToken.objects.filter(user_id=user.id).count()

    return {
        "groups_count": groups_count,
        "permissions_count": permissions_count,
        "api_keys_count": api_keys_count,
        "oauth": {
            "applications": oauth_apps_count,
            "access_tokens": oauth_access_tokens,
            "refresh_tokens": oauth_refresh_tokens,
            "grants": oauth_grants,
            "id_tokens": oauth_id_tokens,
        },
    }


def section_topics(*, user: CustomUser, **_):
    return {
        "topics_total": Topic.objects.filter(user_id=user.id).count(),
        "queries_total": Query.objects.filter(user_id=user.id).count(),
        "db_connections_total": DatabaseConnection.objects.filter(owner_id=user.id).count(),
    }


def section_queries(*, user: CustomUser, **_):
    # Top queries by frequency (safe default)
    return {
        "top_queries": list(
            SearchQueryLog.objects.filter(user_id=user.id)
            .values("query_text")
            .annotate(count=Count("id"))
            .order_by("-count")[:20]
        )
    }


def section_endpoints(*, user: CustomUser, **_):
    qs = EndpointResponseTable.objects.filter(run__user_id=user.id)
    breakdown = list(
        qs.values("endpoint_name", "status").annotate(count=Count("id")).order_by("-count")
    )
    return {"total": qs.count(), "breakdown": breakdown}


def section_insights(*, user: CustomUser, **_):
    latest = (
        UserInsights.objects.filter(user_id=user.id)
        .order_by("-generated_at")
        .values("generated_at", "generated_async", "task_id")
        .first()
    )
    client_settings = None
    if getattr(user, "client_id", None):
        client_settings = (
            SystemSettings.objects.filter(client_id=user.client_id)
            .values(
                "site_name", "default_language", "dark_mode", "maintenance_mode",
                "public_registration", "api_rate_limit", "default_timezone",
                "enable_iso_logging", "enable_hipaa_mode"
            )
            .first()
        )
    return {
        "latest_meta": latest,
        "client_settings": client_settings,
        "admin_logs_count": AdminLogEntry.objects.filter(user_id=user.id).count(),
        "admin_logs_by_content_type": _content_type_map_for_user_logs(user.id),
        "webhooks_configured": Webhook.objects.filter(user_id=user.id).count(),
    }


def section_highlights(
    *,
    user: CustomUser,
    project_id: Optional[str] = None,
    service_id: Optional[str] = None,
    since: Optional[str] = None,
    **_,
):
    """
    Quality-of-life KPIs for the home dashboard:
    - last 7d activity counts
    - top file types / extensions
    - largest & most recent files
    - endpoint error signal (last 7d)
    - hot hours (searches, last 30d)
    - storage growth (bytes/day, last 30d)
    """
    now = _utcnow()
    last7 = now - timedelta(days=7)
    last30 = now - timedelta(days=30)

    # Scope files by project/service when present
    files_qs = _file_base_qs(user, project_id, service_id)

    # ---- Last 7d activity
    files_last7 = files_qs.filter(created_at__gte=last7).count()
    runs_last7 = Run.objects.filter(user_id=user.id, created_at__gte=last7).count()
    searches_last7 = SearchQueryLog.objects.filter(user_id=user.id, created_at__gte=last7).count()
    api_calls_last7 = UserAPICall.objects.filter(user_id=user.id, timestamp__gte=last7).count()
    tokens_last7 = _sum_bigint(
        TokenUsage.objects.filter(user_id=user.id, created_at__gte=last7),
        "tokens_used",
    )

    # ---- Top file types / extensions (scoped)
    top_doc_types = list(
        files_qs.values("document_type")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )
    top_extensions = list(
        files_qs.values("extension")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )

    # ---- Largest & most recent files (scoped)
    largest_files = list(
        files_qs.values("id", "filename", "file_size", "created_at")
        .order_by("-file_size", "-created_at")[:5]
    )
    recent_files = list(
        files_qs.values("id", "filename", "file_size", "created_at")
        .order_by("-created_at")[:5]
    )

    # ---- Endpoint error signal (last 7d)
    ep_7d = EndpointResponseTable.objects.filter(run__user_id=user.id, created_at__gte=last7)
    # Treat non-success statuses as errors; tweak list if you have other success flags
    err_7d = ep_7d.exclude(status__in=["Completed", "Success", "OK"])
    denom = ep_7d.count() or 1  # avoid div-by-zero
    error_rate_7d = round((err_7d.count() / denom) * 100, 2)
    recent_errors = list(
        err_7d.values("endpoint_name", "status", "created_at")
        .order_by("-created_at")[:10]
    )

    # ---- Hot hours (searches, last 30d) — hour-of-day histogram 0..23
    hot_hours_rows = list(
        SearchQueryLog.objects.filter(user_id=user.id, created_at__gte=last30)
        .annotate(h=ExtractHour("created_at"))
        .values("h")
        .annotate(count=Count("id"))
        .order_by("h")
    )
    # Normalize to always include all hours 0..23
    hot_hours = {i: 0 for i in range(24)}
    for r in hot_hours_rows:
        if r["h"] is not None:
            hot_hours[int(r["h"])] = r["count"]

    # ---- Storage growth (bytes/day, last 30d) — scoped to files
    storage_growth = list(
        files_qs.filter(created_at__gte=last30)
        .annotate(day=TruncDay("created_at"))
        .values("day")
        .annotate(
            bytes=Coalesce(
                Sum("file_size", output_field=BigIntegerField()),
                Value(0, output_field=BigIntegerField()),
            )
        )
        .order_by("day")
    )

    # ---- Simple merged recent activity feed (latest 15 across sources)
    ua = list(
        UserActivityLog.objects.filter(user_id=user.id)
        .values("event", "timestamp")
        .order_by("-timestamp")[:10]
    )
    for x in ua:
        x["_kind"] = "user_activity"
        x["_ts"] = x.pop("timestamp", None)

    fal = list(
        FileAuditLog.objects.filter(Q(user_id=user.id) | Q(file__user_id=user.id))
        .values("action", "timestamp", "file_id")
        .order_by("-timestamp")[:10]
    )
    for x in fal:
        x["_kind"] = "file_audit"
        x["_ts"] = x.pop("timestamp", None)

    fev = list(
        FileEventLog.objects.filter(Q(triggered_by_id=user.id) | Q(file__user_id=user.id))
        .values("event_type", "timestamp", "file_id")
        .order_by("-timestamp")[:10]
    )
    for x in fev:
        x["_kind"] = "file_event"
        x["_ts"] = x.pop("timestamp", None)

    ep = list(
        EndpointResponseTable.objects.filter(run__user_id=user.id)
        .values("endpoint_name", "status", "created_at")
        .order_by("-created_at")[:10]
    )
    for x in ep:
        x["_kind"] = "endpoint"
        x["_ts"] = x.pop("created_at", None)

    feed = ua + fal + fev + ep
    feed.sort(key=lambda r: r.get("_ts") or now, reverse=True)
    recent_activity = feed[:15]

    return {
        "last_7d": {
            "files": files_last7,
            "runs": runs_last7,
            "searches": searches_last7,
            "api_calls": api_calls_last7,
            "tokens_used": int(tokens_last7),
            "endpoint_error_rate_pct": error_rate_7d,
        },
        "top": {
            "document_types": top_doc_types,
            "extensions": top_extensions,
        },
        "files": {
            "largest": largest_files,
            "recent": recent_files,
        },
        "endpoints": {
            "recent_errors": recent_errors,
        },
        "search_hot_hours_last_30d": hot_hours,  # dict: hour -> count
        "storage_growth_30d": storage_growth,     # list of {day, bytes}
        "recent_activity": recent_activity,       # mixed feed, newest first
    }



# ── Overview & extras ────────────────────────────────────────────────────────
def build_overview(*, user: CustomUser, project_id: Optional[str] = None,
                   service_id: Optional[str] = None, since: Optional[str] = None, **_):
    """Build the full dashboard payload by composing sections."""
    return {
        "user": section_user(user=user, since=since),
        "files": section_files(user=user, project_id=project_id, service_id=service_id, since=since),
        "runs": section_runs(user=user, since=since),
        "storage": section_storage(user=user, since=since),
        "search": section_search(user=user, since=since),
        "ocr": section_ocr(user=user, project_id=project_id, service_id=service_id, since=since),
        "translation": section_translation(user=user, project_id=project_id, service_id=service_id, since=since),
        "operations": section_operations(user=user, since=since),
        "billing": section_billing(user=user, since=since),
        "integrations": section_integrations(user=user, since=since),
        "security": section_security(user=user),
        "topics": section_topics(user=user),
        "queries": section_queries(user=user),
        "endpoints": section_endpoints(user=user),
        "insights": section_insights(user=user),
        "highlights": section_highlights(   # <— NEW
            user=user, project_id=project_id, service_id=service_id, since=since
        ),
        "window": {"since": since or (_utcnow() - timedelta(days=DEFAULT_WINDOW_DAYS))},
    }


def slice_section(payload: dict, path: Iterable[str]) -> dict:
    """Safely fetch a nested dict section by path."""
    node = payload
    for key in path:
        node = node.get(key, {})
        if not isinstance(node, dict) and key != path[-1]:
            return {}
    return node if isinstance(node, dict) else {path[-1]: node}


def build_cards(user: CustomUser, project_id: Optional[str] = None, service_id: Optional[str] = None):
    files = section_files(user=user, project_id=project_id, service_id=service_id)
    runs = section_runs(user=user)
    billing = section_billing(user=user)
    search = section_search(user=user)
    return {
        "total_files": files.get("total", 0),
        "storage_bytes": files.get("storage_bytes", 0),
        "runs_total": runs.get("total", 0),
        "search_total": search.get("total", 0),
        "tokens_total": billing.get("tokens_total", 0),
    }


def build_timeseries(user: CustomUser, range: Optional[str] = None,
                     date_from: Optional[str] = None, date_to: Optional[str] = None):
    """Minimal placeholder: files created per month (last 6 months)."""
    qs = File.objects.filter(user_id=user.id, created_at__gte=_utcnow() - timedelta(days=180))
    points = list(
        qs.annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )
    return {"files_created": points}


def build_top_files(user: CustomUser, limit: int = 10):
    """Top files by size (fallback to latest if size ties)."""
    return list(
        _file_base_qs(user, None, None)
        .values("id", "filename", "file_size", "created_at")
        .order_by("-file_size", "-created_at")[: int(limit)]
    )


def build_top_searches(user: CustomUser, limit: int = 10):
    return list(
        SearchQueryLog.objects.filter(user_id=user.id)
        .values("query_text")
        .annotate(count=Count("id"))
        .order_by("-count")[: int(limit)]
    )

# ── Celery-friendly entrypoint used by tasks.py ───────────────────────────────
from typing import Optional, Dict, Any  # (redundant if already imported above)

def gather_user_dashboard_metrics(
    *,
    user: Optional[CustomUser] = None,
    user_id: Optional[int] = None,
    client_id: Optional[str] = None,
    project_id: Optional[str] = None,
    service_id: Optional[str] = None,
    since: Optional[str] = None,
    **_,
) -> Dict[str, Any]:
    """
    Resolve the user from a trusted object or worker-supplied user_id and build the
    full dashboard metrics payload. This wrapper exists so Celery can import
    a single symbol without pulling in the Celery app (avoids circulars).
    """
    # Resolve user if not provided
    if user is None:
        if user_id is not None:
            user = CustomUser.objects.filter(id=user_id).first()
        else:
            return {
                "error": "MISSING_USER",
                "user_id": user_id,
                "client_id": client_id,
                "window": {"since": since or (_utcnow() - timedelta(days=DEFAULT_WINDOW_DAYS))},
            }

    if user is None:
        return {
            "error": "USER_NOT_FOUND",
            "user_id": user_id,
            "client_id": client_id,
            "window": {"since": since or (_utcnow() - timedelta(days=DEFAULT_WINDOW_DAYS))},
        }

    # Delegate to the existing builder
    return build_overview(
        user=user,
        project_id=project_id,
        service_id=service_id,
        since=since,
    )

# Ensure it’s exported if the module uses __all__
try:
    __all__  # type: ignore[name-defined]
except NameError:
    __all__ = []
__all__ += ["gather_user_dashboard_metrics"]


# Map for /section/<key>/ in views.py
SECTIONS = {
    "user": section_user,
    "files": section_files,
    "runs": section_runs,
    "storage": section_storage,
    "search": section_search,
    "ocr": section_ocr,
    "translation": section_translation,
    "operations": section_operations,
    "billing": section_billing,
    "integrations": section_integrations,
    "security": section_security,
    "topics": section_topics,
    "queries": section_queries,
    "endpoints": section_endpoints,
    "insights": section_insights,
    "highlights": section_highlights,
}

