"""
AI spend for executives (Home, AI spend) and the limits of the AI gateway.

GET    /api/v1/home-analytics/ai-spend/?range=this_month            presets: see gateway.RANGES
GET    /api/v1/home-analytics/ai-spend/?start=2026-10-01&end=2026-10-31   any dates (inclusive)
PUT    /api/v1/home-analytics/ai-spend/limits/   {scope: user|key, user_id | key_id, period, unit, amount, enabled, alert_percent}
PATCH  /api/v1/home-analytics/ai-spend/limits/<id>/   any of: enabled, amount, period, unit, alert_percent
DELETE /api/v1/home-analytics/ai-spend/limits/<id>/

Seeing spend needs cost_centre.view (Executive, Manager, Admin); changing limits needs
cost_centre.manage (Executive, Admin). Everything is limited to the caller's organisation.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id, user_has_perm
from .agents import events, gateway
from .models import AISpendLimit


def can_view(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "cost_centre.view")


def can_manage(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "cost_centre.manage")


def _forbidden(message):
    return Response({"error": message}, status=status.HTTP_403_FORBIDDEN)


def _bad(message):
    return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)


class AISpendView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user, tid = request.user, tenant_id(request.user)
        if not can_view(user):
            return _forbidden("AI spend is shown to executives, managers and administrators.")
        if tid is None:
            return _bad("Your account is not part of an organisation.")
        preset = request.query_params.get("range")
        if preset and preset not in gateway.RANGES:
            return _bad(f"range must be one of: {', '.join(gateway.RANGES)}.")
        try:
            first, last, label = gateway.resolve_range(preset, request.query_params.get("start"),
                                                      request.query_params.get("end"))
        except ValueError as exc:
            return _bad(str(exc) if "range of at most" in str(exc) else "Dates must be YYYY-MM-DD.")
        data = gateway.report(tid, first, last, label)
        data["can_manage"] = can_manage(user)
        data["ranges"] = list(gateway.RANGES)
        return Response(data)


def _clean(data, partial=False):
    """Validated limit fields from a request body (only the provided ones when partial)."""
    out = {}
    if not partial or "period" in data:
        out["period"] = str(data.get("period") or "month")
        if out["period"] not in dict(AISpendLimit.PERIODS):
            raise ValueError("period must be day, week or month.")
    if not partial or "unit" in data:
        out["unit"] = str(data.get("unit") or "usd")
        if out["unit"] not in dict(AISpendLimit.UNITS):
            raise ValueError("unit must be usd or tokens.")
    if not partial or "amount" in data:
        try:
            amount = Decimal(str(data.get("amount")))
        except (InvalidOperation, TypeError):
            raise ValueError("amount must be a number.")
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000000"):
            raise ValueError("amount must be more than 0.")
        out["amount"] = amount.quantize(Decimal("0.01"))
    if not partial or "alert_percent" in data:
        try:
            out["alert_percent"] = int(data.get("alert_percent", 80))
        except (TypeError, ValueError):
            raise ValueError("alert_percent must be a whole number.")
        if not 1 <= out["alert_percent"] <= 100:
            raise ValueError("alert_percent must be between 1 and 100.")
    if not partial or "enabled" in data:
        value = data.get("enabled", True)
        out["enabled"] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    return out


def _subject(tid, data):
    """(scope, user, key_id) for a new limit, checked against the caller's organisation."""
    scope = str(data.get("scope") or "")
    if scope == "user":
        try:
            user = get_user_model().objects.get(pk=int(data.get("user_id")), client_id=tid)
        except (TypeError, ValueError, get_user_model().DoesNotExist):
            raise ValueError("Choose a person in your organisation.")
        return scope, user, ""
    if scope == "key":
        key_id = str(data.get("key_id") or "")
        allowed = {gateway.PLATFORM_KEY, f"org:{tid}"}
        if key_id.startswith("user:") and key_id[5:].isdigit() and \
                get_user_model().objects.filter(pk=int(key_id[5:]), client_id=tid).exists():
            allowed.add(key_id)
        if key_id not in allowed:
            raise ValueError("Choose one of your organisation's AI keys.")
        return scope, None, key_id
    raise ValueError("scope must be user or key.")


class AISpendLimitView(APIView):
    permission_classes = [IsAuthenticated]

    def _guard(self, request):
        if not can_manage(request.user):
            return _forbidden("Only executives and administrators can change AI limits.")
        if tenant_id(request.user) is None:
            return _bad("Your account is not part of an organisation.")
        return None

    def _respond(self, limit, request, change):
        events.emit("cost", f"limit.{change}", tenant_id=limit.tenant_id, by=request.user.email,
                    scope=limit.scope, user_id=limit.user_id, key_id=limit.key_id, period=limit.period,
                    unit=limit.unit, amount=str(limit.amount), enabled=limit.enabled)
        return Response(gateway.serialize_limit(limit))

    def put(self, request):
        if (denied := self._guard(request)):
            return denied
        tid = tenant_id(request.user)
        try:
            scope, user, key_id = _subject(tid, request.data)
            fields = _clean(request.data)
        except ValueError as exc:
            return _bad(str(exc))
        lookup = {"tenant_id": tid, "scope": scope, **({"user": user} if scope == "user" else {"key_id": key_id})}
        limit, created = AISpendLimit.objects.update_or_create(**lookup, defaults={**fields, "updated_by": request.user})
        return self._respond(limit, request, "created" if created else "changed")

    def patch(self, request, pk):
        if (denied := self._guard(request)):
            return denied
        limit = AISpendLimit.objects.filter(pk=pk, tenant_id=tenant_id(request.user)).first()
        if limit is None:
            return Response({"error": "Limit not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            fields = _clean(request.data, partial=True)
        except ValueError as exc:
            return _bad(str(exc))
        for name, value in fields.items():
            setattr(limit, name, value)
        limit.updated_by = request.user
        limit.save()
        return self._respond(limit, request, "changed")

    def delete(self, request, pk):
        if (denied := self._guard(request)):
            return denied
        limit = AISpendLimit.objects.filter(pk=pk, tenant_id=tenant_id(request.user)).first()
        if limit is None:
            return Response({"error": "Limit not found."}, status=status.HTTP_404_NOT_FOUND)
        events.emit("cost", "limit.removed", tenant_id=limit.tenant_id, by=request.user.email,
                    scope=limit.scope, user_id=limit.user_id, key_id=limit.key_id)
        limit.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
