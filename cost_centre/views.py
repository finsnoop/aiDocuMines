# cost_centre/views.py
from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from django.utils import timezone
from django.db.models import Sum
from django.conf import settings
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EventLog, TokenUsage, Budget, Subscription, PaymentHistory
from .serializers import (
    EventLogSerializer,
    TokenUsageSerializer,
    BudgetSerializer,
    SubscriptionSerializer,
    PaymentHistorySerializer,
)
from . import utils
from .tasks import (
    preflight_check_task,
    finalize_and_bill_task,
    record_event_and_usage_task,
    stripe_sync_subscription_status_task,
)

# ----- Your role permissions -----
from custom_authentication.permissions import (
    IsAdminOrSuperUser,
    IsGroupMemberAny,
    IsClientOrAdminOrManagerOrDeveloper,
)


from django.contrib.auth import get_user_model
User = get_user_model()


logger = logging.getLogger(__name__)


def _idem_key(request) -> Optional[str]:
    return request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")


def _ensure_tenant(user):
    utils.get_tenant_for_user(user)  # raises if user has no tenant/client


def _as_bool(request, key: str) -> bool:
    # works for query params or JSON body
    val = request.query_params.get(key, request.data.get(key))
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in {"1", "true", "yes", "on"}
    return False


def _build_usage_summary(user) -> Dict[str, Any]:
    """Compute cycle totals + overage snapshot for current user/tenant."""
    tenant = utils.get_tenant_for_user(user)
    plan = utils.get_plan_for_user(user)
    start, end = utils.current_billing_window()

    used_tokens = utils.aggregate_tokens(user, tenant, start, end)
    used_pages = utils.aggregate_pages(tenant, start, end)
    used_storage_gb = utils.aggregate_storage_gb(tenant)

    token_over = utils.tokens_overage_cost(plan, used_tokens)
    page_over = utils.pages_overage_cost(plan, used_pages)
    storage_over = utils.storage_overage_cost(plan, used_storage_gb)

    return {
        "cycle": {"start": start, "end": end},
        "used": {
            "tokens": used_tokens,
            "pages": used_pages,
            "storage_gb": used_storage_gb,
        },
        "overage": {
            "tokens_usd": str(token_over),
            "pages_usd": str(page_over),
            "storage_usd": str(storage_over),
            "currency": "USD",
        },
        "plan": {
            "code": plan.code,
            "name": plan.name,
            "included": {
                "tokens": plan.tokens_included,
                "pages": plan.pages_included,
                "storage_gb": plan.storage_gb_included,
            },
        },
    }


# =========================
# Catalog (plans + services)
# =========================
class CatalogView(APIView):
    """
    GET /api/cost/catalog/
    Returns available plan catalog and service registry (for UI).
    """
    permission_classes = [IsAuthenticated, IsGroupMemberAny]

    def get(self, request):
        _ensure_tenant(request.user)
        plans = []
        for code, p in utils.PLAN_CATALOG.items():
            plans.append(
                {
                    "code": p.code,
                    "name": p.name,
                    "price_per_user_month": str(p.price_per_user_month),
                    "pages_included": p.pages_included,
                    "tokens_included": p.tokens_included,
                    "storage_gb_included": p.storage_gb_included,
                    "highlights": p.highlights,
                }
            )
        services = []
        for code, s in utils.SERVICE_REGISTRY.items():
            services.append(
                {
                    "code": s.code,
                    "name": s.name,
                    "payable": s.payable,
                    "price_per_1k_tokens": str(s.price_per_1k_tokens),
                    "currency": s.currency,
                    "stripe_item_key": s.stripe_item_key,
                }
            )
        return Response({"plans": plans, "services": services})


# =========================
# Events (read + record)
# =========================
class EventLogViewSet(mixins.ListModelMixin,
                      mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """
    GET  /api/cost/events/
    GET  /api/cost/events/{id}/
    POST /api/cost/events/record/ { service_code, tokens_used, metadata?, idempotency_key? }
    """
    serializer_class = EventLogSerializer
    permission_classes = [IsAuthenticated, IsGroupMemberAny]

    def get_queryset(self):
        user = self.request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        qs = EventLog.objects.filter(user=user, tenant=tenant).order_by("-created_at")

        q = self.request.query_params
        if t := q.get("type"):
            qs = qs.filter(event_type=t)
        if f := q.get("from"):
            qs = qs.filter(created_at__gte=f)
        if to := q.get("to"):
            qs = qs.filter(created_at__lte=to)
        return qs

    @action(detail=False, methods=["post"], url_path="record",
            permission_classes=[IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper])
    def record(self, request):
        """
        Idempotent event+usage record. Prefer finalize endpoint for full billing,
        but this is useful for simple 'log-only' flows.
        """
        _ensure_tenant(request.user)
        svc = request.data.get("service_code")
        tokens = int(request.data.get("tokens_used") or 0)
        meta = request.data.get("metadata") or {}
        idem = _idem_key(request)

        if _as_bool(request, "async"):
            async_res = record_event_and_usage_task.delay(
                user_id=request.user.id,
                service_code=svc,
                tokens_used=tokens,
                metadata=meta,
                idempotency_key=idem,
            )
            return Response({"task_id": async_res.id}, status=status.HTTP_202_ACCEPTED)

        # sync path
        event, tusage = utils.record_event_and_usage(
            user=request.user,
            service_code=svc,
            tokens_used=tokens,
            metadata=meta,
            idempotency_key=idem,
        )
        payload = {
            "event": EventLogSerializer(event).data,
            "token_usage_id": getattr(tusage, "id", None),
        }
        return Response(payload, status=status.HTTP_201_CREATED)


# =========================
# Token usage (read-only)
# =========================
class TokenUsageViewSet(mixins.ListModelMixin,
                        mixins.RetrieveModelMixin,
                        viewsets.GenericViewSet):
    """
    GET /api/cost/tokens/
    GET /api/cost/tokens/{id}/
    """
    serializer_class = TokenUsageSerializer
    permission_classes = [IsAuthenticated, IsGroupMemberAny]

    def get_queryset(self):
        user = self.request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        qs = TokenUsage.objects.filter(user=user, tenant=tenant).order_by("-created_at")

        q = self.request.query_params
        if f := q.get("from"):
            qs = qs.filter(created_at__gte=f)
        if to := q.get("to"):
            qs = qs.filter(created_at__lte=to)
        return qs


# =========================
# Budget (current user)
# =========================
class BudgetViewSet(viewsets.ModelViewSet):
    """
    GET    /api/cost/budgets/me/
    PATCH  /api/cost/budgets/me/   { token_limit, financial_limit }
    """
    serializer_class = BudgetSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper]

    def get_queryset(self):
        user = self.request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        return Budget.objects.filter(user=user, tenant=tenant)

    @action(detail=False, methods=["get", "patch"], url_path="me",
            permission_classes=[IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper])
    def me(self, request):
        user = request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        obj, _ = Budget.objects.get_or_create(user=user, tenant=tenant)
        if request.method.lower() == "get":
            return Response(BudgetSerializer(obj).data)
        ser = BudgetSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


# =========================
# Subscription (plan/seats/term) + quote + sync
# =========================
class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    GET   /api/cost/subscriptions/me/
    PATCH /api/cost/subscriptions/me/      { plan_code, seat_count, annual_prepay }
    POST  /api/cost/subscriptions/me/quote { plan_code, seat_count, annual_prepay }
    POST  /api/cost/subscriptions/me/sync  {}  (sync status from Stripe)
    """
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper]

    def get_queryset(self):
        user = self.request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        return Subscription.objects.filter(user=user, tenant=tenant)

    @action(detail=False, methods=["get", "patch"], url_path="me",
            permission_classes=[IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper])
    def me(self, request):
        user = request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        sub = (
            Subscription.objects.filter(user=user, tenant=tenant)
            .order_by("-updated_at").first()
            or Subscription.objects.create(
                user=user,
                tenant=tenant,
                stripe_subscription_id="",
                stripe_status="inactive",
                billing_cycle_start=timezone.now(),
                billing_cycle_end=timezone.now(),
            )
        )
        if request.method.lower() == "get":
            return Response(SubscriptionSerializer(sub).data)
        # PATCH: only allow plan/seats/term adjustments
        allowed = {"plan_code", "seat_count", "annual_prepay"}
        changes = {k: v for k, v in request.data.items() if k in allowed}
        if changes:
            for k, v in changes.items():
                setattr(sub, k, v)
            sub.save(update_fields=list(changes.keys()) + ["updated_at"])
        return Response(SubscriptionSerializer(sub).data)

    @action(detail=False, methods=["post"], url_path="me/quote",
            permission_classes=[IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper])
    def quote(self, request):
        user = request.user
        _ensure_tenant(user)
        plan_code = request.data.get("plan_code") or utils.get_plan_for_user(user).code
        seat_count = int(request.data.get("seat_count") or 1)
        annual = bool(request.data.get("annual_prepay") or False)
        return Response(utils.quote_monthly(plan_code, seat_count, annual))

    @action(detail=False, methods=["post"], url_path="me/sync",
            permission_classes=[IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper])
    def sync(self, request):
        """Kick a Stripe sync on the current (or created) subscription."""
        user = request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        sub = (
            Subscription.objects.filter(user=user, tenant=tenant)
            .order_by("-updated_at").first()
        )
        if not sub:
            return Response({"ok": False, "message": "No subscription to sync"}, status=400)

        if _as_bool(request, "async"):
            async_res = stripe_sync_subscription_status_task.delay(subscription_id=sub.id)
            return Response({"task_id": async_res.id}, status=status.HTTP_202_ACCEPTED)

        # sync now
        try:
            import stripe
            stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", None)
            stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id)
            sub.stripe_status = stripe_sub.get("status", sub.stripe_status)
            sub.save(update_fields=["stripe_status", "updated_at"])
            return Response({"ok": True, "status": sub.stripe_status})
        except Exception as e:
            logger.error(f"Stripe sync failed for subscription {sub.id}: {e}")
            return Response({"ok": False, "message": str(e)}, status=500)


# =========================
# Usage summary (cycle totals)
# =========================
class UsageSummaryView(APIView):
    """
    GET /api/cost/summary/
    """
    permission_classes = [IsAuthenticated, IsGroupMemberAny]

    def get(self, request):
        _ensure_tenant(request.user)
        return Response(_build_usage_summary(request.user))


# =========================
# Preflight & Finalize
# =========================
class PreflightCheckView(APIView):
    """
    POST /api/cost/preflight/
    { service_code, est_tokens, est_pages, async? }
    """
    permission_classes = [IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper]

    def post(self, request):
        _ensure_tenant(request.user)
        svc = request.data.get("service_code")
        est_t = int(request.data.get("est_tokens") or 0)
        est_p = int(request.data.get("est_pages") or 0)

        if _as_bool(request, "async"):
            async_res = preflight_check_task.delay(
                user_id=request.user.id, service_code=svc, est_tokens=est_t, est_pages=est_p
            )
            return Response({"task_id": async_res.id}, status=status.HTTP_202_ACCEPTED)

        try:
            utils.enforce_preflight_limits(request.user, svc, est_t, est_p)
            return Response({"ok": True, "message": "Preflight check passed"})
        except Exception as e:
            return Response({"ok": False, "message": str(e)}, status=status.HTTP_403_FORBIDDEN)


class FinalizeUsageView(APIView):
    """
    POST /api/cost/finalize/
    { service_code, actual_tokens_used, actual_pages_processed, metadata, idempotency_key?, async? }
    Idempotency-Key may be sent as a header or in body.
    """
    permission_classes = [IsAuthenticated, IsClientOrAdminOrManagerOrDeveloper]

    def post(self, request):
        _ensure_tenant(request.user)
        svc = request.data.get("service_code")
        toks = int(request.data.get("actual_tokens_used") or 0)
        pages = int(request.data.get("actual_pages_processed") or 0)
        meta = request.data.get("metadata") or {}
        idem = _idem_key(request)

        if _as_bool(request, "async"):
            async_res = finalize_and_bill_task.delay(
                user_id=request.user.id,
                service_code=svc,
                actual_tokens_used=toks,
                actual_pages_processed=pages,
                metadata=meta,
                idempotency_key=idem,
            )
            return Response({"task_id": async_res.id}, status=status.HTTP_202_ACCEPTED)

        summary = utils.finalize_and_bill(
            user=request.user,
            service_code=svc,
            actual_tokens_used=toks,
            actual_pages_processed=pages,
            metadata=meta,
            idempotency_key=idem,
        )
        return Response(summary, status=status.HTTP_201_CREATED)


# =========================
# Tenant admin aggregate
# =========================
class TenantUsageAdminView(APIView):
    """
    GET /api/cost/admin/tenant/summary
    Aggregates the current cycle for this tenant.
    """
    permission_classes = [IsAuthenticated, IsAdminOrSuperUser]

    def get(self, request):
        tenant = utils.get_tenant_for_user(request.user)
        start, end = utils.current_billing_window()
        user_ids = Budget.objects.filter(tenant=tenant).values_list("user_id", flat=True)

        total_tokens = TokenUsage.objects.filter(
            user_id__in=list(user_ids), tenant=tenant, created_at__gte=start, created_at__lt=end
        ).aggregate(s=Sum("tokens_used"))["s"] or 0

        # TODO: implement your real page/storage totals
        total_pages = 0
        storage_gb = 0

        return Response({
            "tenant_id": getattr(tenant, "id", None),
            "cycle_start": start,
            "cycle_end": end,
            "total_tokens": total_tokens,
            "total_pages": total_pages,
            "storage_gb": storage_gb,
            "users_count": len(set(user_ids)),
        })


# =========================
# Payments (read-only)
# =========================
class PaymentHistoryViewSet(mixins.ListModelMixin,
                            mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    """
    GET /api/cost/payments/
    GET /api/cost/payments/{id}/
    """
    serializer_class = PaymentHistorySerializer
    permission_classes = [IsAuthenticated, IsGroupMemberAny]

    def get_queryset(self):
        user = self.request.user
        _ensure_tenant(user)
        tenant = utils.get_tenant_for_user(user)
        return PaymentHistory.objects.filter(user=user, tenant=tenant).order_by("-payment_date")


# =========================
# Stripe webhook (signature verified)
# =========================
class StripeWebhookView(APIView):
    """
    POST /api/cost/stripe/webhook/
    """
    permission_classes = [AllowAny]

    def post(self, request):
        import stripe
        from django.conf import settings

        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        endpoint_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)

        if not endpoint_secret:
            logger.error("STRIPE_WEBHOOK_SECRET not configured")
            return Response(status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        except Exception as e:
            logger.warning(f"Stripe webhook signature verification failed: {e}")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        etype = event.get("type")
        data_obj = event.get("data", {}).get("object", {})

        if etype in ("invoice.paid", "customer.subscription.updated", "customer.subscription.created"):
            stripe_sub_id = data_obj.get("subscription") or data_obj.get("id")
            if stripe_sub_id:
                sub = Subscription.objects.filter(stripe_subscription_id=stripe_sub_id).first()
                if sub:
                    sub.stripe_status = data_obj.get("status", sub.stripe_status)
                    sub.save(update_fields=["stripe_status", "updated_at"])

        elif etype == "invoice.payment_failed":
            # TODO: notify admins, downgrade, etc.
            pass

        return Response(status=status.HTTP_200_OK)




class WhoAmIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        u = request.user
        groups = list(u.groups.values_list("name", flat=True))
        client_id = getattr(u, "client_id", None)
        return Response({
            "id": u.id,
            "email": getattr(u, "email", None),
            "is_superuser": u.is_superuser,
            "is_staff": u.is_staff,
            "client_id": client_id,
            "groups": groups,
        })
