"""Agent operations API for the IT / administration dashboard (Settings)."""
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id
from .agents import cost, events, resources
from .models import AIBudget, ModelPrice


def _is_admin(user):
    return is_tenant_admin(user) or is_platform_superuser(user)


class AgentOperationsView(APIView):
    """GET /api/v1/home-analytics/agents/ : orchestrator, capacity, cost vs budget, recent decisions."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not _is_admin(user):
            return Response({'error': 'Administrators only.'}, status=status.HTTP_403_FORBIDDEN)
        platform = is_platform_superuser(user)
        capacity = resources.assess()
        if not platform:
            # Organisation admins see the platform's health, not its internals or other tenants.
            capacity = {'state': capacity['state'], 'reasons': capacity['reasons']}
        from .agents.orchestrator import agents_ready
        ready, reason = agents_ready(user)
        data = {
            'orchestrator': cache.get('agents:orchestrator-last'),
            'agents_ready': ready, 'agents_reason': reason,
            'resources': capacity,
            'cost': cost.assess(tenant_id(user), emit_changes=False),
            'events': events.recent(tenant_id(user), 50, platform=platform),
            'prices': list(ModelPrice.objects.values('provider', 'model', 'input_usd_per_million',
                                                     'output_usd_per_million', 'source', 'updated_at')),
            'can_edit_prices': platform,
        }
        if platform:
            from cost_centre.models import EventLog
            tenants = EventLog.objects.filter(created_at__gte=cost.month_start()).values_list('tenant_id', flat=True).distinct()
            data['tenants'] = [{'tenant_id': t, **{k: v for k, v in cost.assess(t, emit_changes=False).items()
                                                  if k != 'breakdown'}} for t in tenants]
        return Response(data)


class AIBudgetView(APIView):
    """PUT /api/v1/home-analytics/agents/budget/ {monthly_usd, alert_percent, hard_stop} (organisation admins)."""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        user = request.user
        if not _is_admin(user) or tenant_id(user) is None:
            return Response({'error': 'Organisation administrators only.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            monthly = Decimal(str(request.data.get('monthly_usd')))
            alert = int(request.data.get('alert_percent', 80))
            if not monthly.is_finite() or monthly < 0 or monthly > Decimal('10000000') or not 1 <= alert <= 100:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            return Response({'error': 'monthly_usd must be a positive amount and alert_percent between 1 and 100.'},
                            status=status.HTTP_400_BAD_REQUEST)
        budget, _ = AIBudget.objects.update_or_create(tenant_id=tenant_id(user), defaults={
            'monthly_usd': monthly.quantize(Decimal('0.01')), 'alert_percent': alert,
            'hard_stop': bool(request.data.get('hard_stop', True))})
        events.emit('cost', 'budget.changed', tenant_id=tenant_id(user), by=user.email,
                    monthly_usd=str(budget.monthly_usd), alert_percent=alert, hard_stop=budget.hard_stop)
        return Response(cost.assess(tenant_id(user)))


class ModelPriceView(APIView):
    """PUT /api/v1/home-analytics/agents/prices/ {provider, model, input_usd_per_million, output_usd_per_million, source}."""
    permission_classes = [IsAuthenticated]

    def put(self, request):
        if not is_platform_superuser(request.user):
            return Response({'error': 'Platform administrators only.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            model = str(request.data['model']).strip()[:255]
            provider = (str(request.data.get('provider') or '*').strip() or '*')[:32]
            prices = [Decimal(str(request.data[k])) for k in ('input_usd_per_million', 'output_usd_per_million')]
            if not model or any(not p.is_finite() or p < 0 or p > 100000 for p in prices):
                raise ValueError
        except (KeyError, InvalidOperation, ValueError, TypeError):
            return Response({'error': 'model, input_usd_per_million and output_usd_per_million are required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        price, _ = ModelPrice.objects.update_or_create(provider=provider, model=model, defaults={
            'input_usd_per_million': prices[0], 'output_usd_per_million': prices[1],
            'source': str(request.data.get('source', ''))[:255]})
        events.emit('cost', 'price.changed', by=request.user.email, provider=provider, model=model,
                    input_usd_per_million=str(prices[0]), output_usd_per_million=str(prices[1]))
        return Response({'provider': price.provider, 'model': price.model,
                         'input_usd_per_million': str(price.input_usd_per_million),
                         'output_usd_per_million': str(price.output_usd_per_million), 'source': price.source})
