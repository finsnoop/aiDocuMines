"""
GET /api/v1/core/harness/overview/?days=30[&organisation=<id>]

AI harness monitoring: how every AI feature behaved, from HarnessRun records (no question or answer text).
Organisation administrators see their own organisation; platform administrators see one organisation or the whole
platform, as aggregates only (the client content policy: figures, never content or individual questions).
"""
from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import ai_harness
from core.access import is_platform_superuser, is_tenant_admin, tenant_id
from core.models import HarnessRun

PROBLEM_OUTCOMES = ("withheld", "blocked", "blocked_injection", "error")


def _p95(qs) -> int:
    n = qs.count()
    if not n:
        return 0
    return int(qs.order_by("latency_ms").values_list("latency_ms", flat=True)[min(n - 1, int(n * 0.95))])


def _summary(qs) -> dict:
    agg = qs.aggregate(runs=Count("id"), calls=Sum("calls"), prompt=Sum("prompt_tokens"), completion=Sum("completion_tokens"),
                       latency=Avg("latency_ms"), checked=Sum("claims_checked"), removed=Sum("claims_removed"))
    outcomes = Counter(dict(qs.values_list("outcome").annotate(n=Count("id")).values_list("outcome", "n")))
    runs = agg["runs"] or 0
    return {
        "runs": runs, "outcomes": dict(outcomes),
        "rates": {k: round(outcomes.get(k, 0) / runs, 4) if runs else 0.0
                  for k in ("answered", "withheld", "off_topic", "not_in_context", "no_evidence", "blocked_injection", "error")},
        "model_calls": agg["calls"] or 0, "tokens": (agg["prompt"] or 0) + (agg["completion"] or 0),
        "avg_latency_ms": int(agg["latency"] or 0), "p95_latency_ms": _p95(qs),
        "claims_checked": agg["checked"] or 0, "claims_removed": agg["removed"] or 0,
        "flags": {f: qs.filter(flags__contains=[f]).count() for f in ("injection", "secret_redacted")},
    }


class HarnessOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        platform = is_platform_superuser(user)
        if not (platform or is_tenant_admin(user)):
            return Response({"error": "Only administrators can see AI harness monitoring."}, status=403)
        try:
            days = max(1, min(int(request.query_params.get("days", 30)), 90))
        except ValueError:
            days = 30
        qs = HarnessRun.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
        org = request.query_params.get("organisation")
        scope = "organisation"
        if platform and org in (None, "", "all"):
            scope = "platform"
        elif platform and str(org).isdigit():
            qs = qs.filter(client_id=int(org))
        else:
            qs = qs.filter(client_id=tenant_id(user))

        titles = {p.key: (p.title or p.key) for p in ai_harness.PROFILES.values()}
        kinds = {p.key: p.kind for p in ai_harness.PROFILES.values()}
        profiles = []
        for key in qs.values_list("profile", flat=True).distinct().order_by("profile"):
            profiles.append({"key": key, "title": titles.get(key, key), "kind": kinds.get(key, "ask"),
                             **_summary(qs.filter(profile=key))})
        profiles.sort(key=lambda p: -p["runs"])

        daily = defaultdict(lambda: {"runs": 0, "problems": 0})
        for row in qs.annotate(day=TruncDate("created_at")).values("day", "outcome").annotate(n=Count("id")):
            day = row["day"].isoformat()
            daily[day]["runs"] += row["n"]
            if row["outcome"] in PROBLEM_OUTCOMES:
                daily[day]["problems"] += row["n"]

        # Recent problems: profile, outcome and flags only; who asked is shown to the person's own organisation.
        recent = []
        own_org = tenant_id(user)
        for r in qs.filter(outcome__in=PROBLEM_OUTCOMES).select_related("user")[:20]:
            recent.append({"at": r.created_at, "profile": titles.get(r.profile, r.profile), "outcome": r.outcome,
                           "flags": r.flags, "steps": r.steps[:6],
                           "person": r.user.email if (r.user and r.client_id == own_org and scope == "organisation") else None})
        return Response({"days": days, "scope": scope, "totals": _summary(qs), "profiles": profiles,
                         "daily": [{"date": d, **v} for d, v in sorted(daily.items())], "recent_problems": recent,
                         "registered": [{"key": p.key, "title": p.title or p.key, "kind": p.kind, "surface": p.surface}
                                        for p in ai_harness.PROFILES.values()]})
