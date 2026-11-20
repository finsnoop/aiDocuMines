"""
AI gateway: spend by key and by person, for any date range, with a limit on each that can be switched on and off.

Every model call on the platform goes through two steps here:

1. ``enforce()`` before the call. It refuses the call when the organisation's monthly budget
   (agents/cost.py) or an enabled limit on the person or on the key is used up.
2. ``usage.record()`` after the call writes the ledger row; it stamps the key that was used
   (``current_key()``), so spend can be reported per key.

Keys are identified, never stored: ``platform`` (the platform's own LiteLLM key), ``org:<id>``
(the organisation's key from Settings, AI) and ``user:<id>`` (a person's own key, when the
organisation allows personal keys).

Dollars come from ModelPrice like the rest of the cost agent; tokens of unpriced models are
reported as unpriced and count only toward limits measured in tokens.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, IntegerField, Sum
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, TruncDate
from django.utils import timezone

PLATFORM_KEY = "platform"
_key: ContextVar = ContextVar("ai_gateway_key", default=None)
CHECK_CACHE_SECONDS = 15
MAX_RANGE_DAYS = 400


class GatewayBlocked(ValueError):
    """A limit refuses this call. The message is safe to show to the person."""


# ── Which key a call uses ────────────────────────────────────────────────────────────────────
def key_for(*, org_id=None, personal_user_id=None) -> str:
    if personal_user_id:
        return f"user:{int(personal_user_id)}"
    if org_id:
        return f"org:{int(org_id)}"
    return PLATFORM_KEY


def current_key() -> str:
    return _key.get() or PLATFORM_KEY


@contextmanager
def using_key(key_id: str):
    token = _key.set(key_id or PLATFORM_KEY)
    try:
        yield
    finally:
        _key.reset(token)


def key_label(key_id: str, users: dict | None = None) -> tuple[str, str]:
    """(kind, readable label) for a key id."""
    if key_id.startswith("org:"):
        return "organisation", "Organisation key"
    if key_id == "personal":
        return "personal", "Personal key"
    if key_id.startswith("user:"):
        uid = key_id.split(":", 1)[1]
        who = (users or {}).get(int(uid)) if uid.isdigit() else None
        return "personal", f"Personal key: {who}" if who else "Personal key"
    return "platform", "Platform key (LiteLLM)"


# ── Pricing ──────────────────────────────────────────────────────────────────────────────────
def _usd(provider, model, tokens, prompt, completion, prices):
    """(usd or None, estimated) for one grouped ledger row."""
    from .cost import MILLION, price_for
    cache_key = (provider, model)
    if cache_key not in prices:
        prices[cache_key] = price_for(provider, model)
    price = prices[cache_key]
    if not price or not tokens:
        return None, False
    split = prompt + completion
    usd = (Decimal(prompt) * price.input_usd_per_million + Decimal(completion) * price.output_usd_per_million) / MILLION
    estimated = tokens > split
    if estimated:  # only a total was reported: priced at the mean of the two prices and flagged
        usd += Decimal(tokens - split) * (price.input_usd_per_million + price.output_usd_per_million) / 2 / MILLION
    return usd, estimated


def _grouped(qs, *fields):
    return (qs.values(*fields)
            .annotate(tokens=Coalesce(Sum("tokens_used"), 0), calls=Count("id"),
                      prompt=Coalesce(Sum(Cast(KT("metadata__prompt_tokens"), IntegerField())), 0),
                      completion=Coalesce(Sum(Cast(KT("metadata__completion_tokens"), IntegerField())), 0)))


def _usage_since(tenant_id, since, **filters):
    """(usd, tokens) since a moment for one person or one key in an organisation."""
    from cost_centre.models import EventLog
    qs = EventLog.objects.filter(tenant_id=tenant_id, created_at__gte=since, **filters)
    usd, tokens, prices = Decimal(0), 0, {}
    for r in _grouped(qs, "metadata__provider", "metadata__model"):
        value, _ = _usd(r["metadata__provider"] or "", r["metadata__model"] or "", int(r["tokens"]),
                        int(r["prompt"]), int(r["completion"]), prices)
        usd += value or 0
        tokens += int(r["tokens"])
    return usd, tokens


# ── Periods ──────────────────────────────────────────────────────────────────────────────────
def period_start(period: str, now=None):
    local = timezone.localtime(now or timezone.now())
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":
        return day - dt.timedelta(days=day.weekday())
    return day.replace(day=1)


PERIOD_LABELS = {"day": "daily", "week": "weekly", "month": "monthly"}


def _ledger_filter(limit):
    if limit.scope == "user":
        return {"user_id": limit.user_id}
    return {"metadata__key": limit.key_id}


def limit_status(limit, now=None):
    """Spent in the limit's current period, percentage and state (ok, warning, exhausted, off)."""
    since = period_start(limit.period, now)
    filters = _ledger_filter(limit)
    usd, tokens = _usage_since(limit.tenant_id, since, **filters)
    if limit.key_id == PLATFORM_KEY and limit.scope == "key":
        # Rows written before keys were recorded have no key; they were all platform-key calls.
        from cost_centre.models import EventLog
        legacy = EventLog.objects.filter(tenant_id=limit.tenant_id, created_at__gte=since, metadata__key__isnull=True)
        if legacy.exists():
            extra_usd, extra_tokens = _usage_since(limit.tenant_id, since, metadata__key__isnull=True)
            usd, tokens = usd + extra_usd, tokens + extra_tokens
    spent = Decimal(tokens) if limit.unit == "tokens" else usd
    used = float(spent / limit.amount * 100) if limit.amount else 100.0
    state = "off" if not limit.enabled else "exhausted" if used >= 100 else "warning" if used >= limit.alert_percent else "ok"
    return {"since": since.isoformat(), "spent_usd": str(usd.quantize(Decimal("0.01"))), "spent_tokens": tokens,
            "used_percent": round(used, 1), "state": state}


def serialize_limit(limit, status=None):
    status = status or limit_status(limit)
    return {"id": limit.id, "scope": limit.scope, "user_id": limit.user_id, "key_id": limit.key_id,
            "period": limit.period, "unit": limit.unit, "amount": str(limit.amount), "enabled": limit.enabled,
            "alert_percent": limit.alert_percent, **status}


# ── Enforcement ──
def ledger_changed(tenant_id):
    """Called after a usage row is written, so limit checks never use a figure from before it."""
    if tenant_id is not None:
        cache.set(f"ai-gw-ledger:{tenant_id}", timezone.now().timestamp(), None)


def check(user=None, key_id=None) -> tuple[bool, str]:
    """May this person spend on this key right now? Includes the organisation's monthly budget."""
    from ..models import AISpendLimit
    from .cost import allow
    tenant_id = getattr(user, "client_id", None) if user is not None else None
    if tenant_id is None:
        return True, ""  # unattributed system work: nothing to limit against
    allowed, why = allow(tenant_id)
    if not allowed:
        return False, why
    key_id = key_id or current_key()
    limits = AISpendLimit.objects.filter(tenant_id=tenant_id, enabled=True)
    limits = [l for l in limits if (l.scope == "user" and l.user_id == user.pk) or (l.scope == "key" and l.key_id == key_id)]
    for limit in limits:
        version = cache.get(f"ai-gw-ledger:{tenant_id}", 0)
        cache_key = f"ai-gw:{limit.pk}:{limit.updated_at.timestamp()}:{period_start(limit.period).date()}:{version}"
        status = cache.get(cache_key)
        if status is None:
            status = limit_status(limit)
            cache.set(cache_key, status, CHECK_CACHE_SECONDS)
        _emit_change(limit, status)
        if status["state"] == "exhausted":
            amount = f"{int(limit.amount):,} tokens" if limit.unit == "tokens" else f"USD {limit.amount}"
            whose = "Your" if limit.scope == "user" else f"The {key_label(limit.key_id)[1].lower()}'s"
            return False, (f"{whose} {PERIOD_LABELS[limit.period]} AI limit ({amount}) is used up. "
                           "An executive or administrator can raise or switch off the limit on Home, AI spend.")
    return True, ""


def enforce(user=None, key_id=None):
    """Raise GatewayBlocked when a limit refuses the call. Uses the current actor when no user is given."""
    if user is None:
        from .usage import actor
        user = actor.get()
    allowed, why = check(user, key_id)
    if not allowed:
        raise GatewayBlocked(why)


def _emit_change(limit, status):
    from .events import emit
    key = f"ai-gw-state:{limit.pk}"
    previous = cache.get(key)
    if previous != status["state"]:
        cache.set(key, status["state"], None)
        if status["state"] in ("warning", "exhausted"):
            emit("cost", f"limit.{status['state']}", tenant_id=limit.tenant_id,
                 severity="critical" if status["state"] == "exhausted" else "warning",
                 scope=limit.scope, user_id=limit.user_id, key_id=limit.key_id, period=limit.period,
                 unit=limit.unit, amount=str(limit.amount), used_percent=status["used_percent"])


# ── Reporting ────────────────────────────────────────────────────────────────────────────────
RANGES = ("today", "yesterday", "7d", "30d", "this_week", "this_month", "last_month", "this_quarter", "this_year")


def resolve_range(preset=None, start=None, end=None, now=None):
    """(first day, last day, label) as local dates, inclusive. Custom dates win over a preset."""
    today = timezone.localtime(now or timezone.now()).date()
    if start or end:
        first = dt.date.fromisoformat(start) if start else today
        last = dt.date.fromisoformat(end) if end else today
        if last < first:
            first, last = last, first
        if (last - first).days > MAX_RANGE_DAYS:
            raise ValueError(f"Choose a range of at most {MAX_RANGE_DAYS} days.")
        return first, last, f"{first.isoformat()} to {last.isoformat()}"
    preset = preset or "this_month"
    if preset == "today":
        return today, today, "Today"
    if preset == "yesterday":
        y = today - dt.timedelta(days=1)
        return y, y, "Yesterday"
    if preset == "7d":
        return today - dt.timedelta(days=6), today, "Last 7 days"
    if preset == "30d":
        return today - dt.timedelta(days=29), today, "Last 30 days"
    if preset == "this_week":
        return today - dt.timedelta(days=today.weekday()), today, "This week"
    if preset == "last_month":
        last = today.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=1), last, "Last month"
    if preset == "this_quarter":
        q = (today.month - 1) // 3 * 3 + 1
        return today.replace(month=q, day=1), today, "This quarter"
    if preset == "this_year":
        return today.replace(month=1, day=1), today, "This year"
    return today.replace(day=1), today, "This month"


def report(tenant_id, first: dt.date, last: dt.date, label: str = ""):
    from cost_centre.models import EventLog
    from django.contrib.auth import get_user_model
    from ..models import AISpendLimit
    from .cost import assess

    tz = timezone.get_current_timezone()
    since = timezone.make_aware(dt.datetime.combine(first, dt.time.min), tz)
    until = timezone.make_aware(dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min), tz)
    qs = EventLog.objects.filter(tenant_id=tenant_id, created_at__gte=since, created_at__lt=until)
    rows = _grouped(qs.annotate(day=TruncDate("created_at", tzinfo=tz)),
                    "day", "user_id", "metadata__key", "metadata__provider", "metadata__model", "metadata__task")

    users = {u.pk: u for u in get_user_model().objects.filter(client_id=tenant_id, is_active=True)
             .only("id", "email", "username", "first_name", "last_name").order_by("email")[:500]}
    # input_tokens / output_tokens as providers report them; unsplit_tokens when a provider gave only a total.
    blank = lambda: {"usd": Decimal(0), "tokens": 0, "input_tokens": 0, "output_tokens": 0, "unsplit_tokens": 0,
                     "calls": 0, "unpriced_tokens": 0}
    days, by_user, by_key, by_model = defaultdict(blank), defaultdict(blank), defaultdict(blank), defaultdict(blank)
    totals, prices, estimated = blank(), {}, False
    for r in rows:
        provider, model = r["metadata__provider"] or "", r["metadata__model"] or ""
        tokens, calls, prompt, completion = int(r["tokens"]), int(r["calls"]), int(r["prompt"]), int(r["completion"])
        usd, est = _usd(provider, model, tokens, prompt, completion, prices)
        estimated = estimated or est
        key_id = r["metadata__key"] or PLATFORM_KEY
        for bucket in (totals, days[r["day"]], by_user[r["user_id"]], by_key[key_id],
                       by_model[(provider or "unattributed", model or "unattributed", r["metadata__task"] or "unattributed")]):
            bucket["tokens"] += tokens
            bucket["input_tokens"] += prompt
            bucket["output_tokens"] += completion
            bucket["unsplit_tokens"] += max(tokens - prompt - completion, 0)
            bucket["calls"] += calls
            if usd is None:
                bucket["unpriced_tokens"] += tokens
            else:
                bucket["usd"] += usd

    money = lambda b: {**b, "usd": str(b["usd"].quantize(Decimal("0.01")))}
    limits = list(AISpendLimit.objects.filter(tenant_id=tenant_id))
    limit_of = {(l.scope, l.user_id if l.scope == "user" else l.key_id): l for l in limits}

    def with_limit(entry, scope, subject):
        limit = limit_of.get((scope, subject))
        entry["limit"] = serialize_limit(limit) if limit else None
        return entry

    def display(u):
        name = f"{u.first_name} {u.last_name}".strip()
        return name or u.username or u.email

    user_rows = []
    for uid in set(users) | {k for k in by_user if k is not None}:
        u = users.get(uid)
        user_rows.append(with_limit({"user_id": uid, "email": u.email if u else "", "name": display(u) if u else "Former user",
                                     **money(by_user.get(uid, blank()))}, "user", uid))
    user_rows.sort(key=lambda r: (-Decimal(r["usd"]), -r["tokens"], r["email"]))

    emails = {uid: u.email for uid, u in users.items()}
    from custom_authentication.policies import organisation_ai_key
    key_ids = set(by_key) | {PLATFORM_KEY} | {l.key_id for l in limits if l.scope == "key"}
    if organisation_ai_key(tenant_id):
        key_ids.add(f"org:{tenant_id}")
    key_rows = []
    for kid in key_ids:
        kind, name = key_label(kid, emails)
        key_rows.append(with_limit({"key_id": kid, "kind": kind, "label": name, **money(by_key.get(kid, blank()))}, "key", kid))
    key_rows.sort(key=lambda r: (-Decimal(r["usd"]), -r["tokens"], r["label"]))

    model_rows = [{"provider": p, "model": m, "task": t, **money(b)} for (p, m, t), b in by_model.items()]
    model_rows.sort(key=lambda r: (-Decimal(r["usd"]), -r["tokens"]))

    series, day = [], first
    while day <= last:
        b = days.get(day, blank())
        series.append({"date": day.isoformat(), "usd": str(b["usd"].quantize(Decimal("0.01"))), "tokens": b["tokens"],
                       "input_tokens": b["input_tokens"], "output_tokens": b["output_tokens"], "calls": b["calls"]})
        day += dt.timedelta(days=1)

    return {"range": {"start": first.isoformat(), "end": last.isoformat(), "label": label or f"{first} to {last}"},
            "totals": {**money(totals), "estimated": estimated}, "series": series,
            "by_user": user_rows, "by_key": key_rows, "by_model": model_rows,
            "budget": assess(tenant_id, emit_changes=False)}
