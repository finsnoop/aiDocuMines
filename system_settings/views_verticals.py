"""Industry vertical switches (system_settings/verticals.py)."""
import logging

from django.db import transaction
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, tenant_id
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from custom_authentication.models import Client
from core.service_catalog import ALL_SERVICES

from . import verticals
from .models import VerticalSetting

logger = logging.getLogger(__name__)


class MyVerticalsView(APIView):
    """GET /api/v1/system-settings/verticals/ : the verticals switched on for the caller's organisation."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def get(self, request):
        access = verticals.access_for(request.user)
        on = access['any']
        return Response({
            'enabled': sorted(on),                      # verticals with at least one feature on
            'verticals_on': sorted(access['verticals']),  # verticals switched on as a whole
            'services': sorted(access['services']),     # every service/feature switched on
            # catalogue services switched off: pages hide these even when their vertical has other features on
            'hidden_services': sorted({s['service_type'] for s in ALL_SERVICES} - access['services']),
            'verticals': [{'key': v['key'], 'label': v['label'], 'industry': v['industry'],
                           'prefixes': list(v['prefixes']), 'enabled': v['key'] in on} for v in verticals.VERTICALS],
        })


class VerticalAdminView(APIView):
    """
    Platform superusers only.
    GET  ?tenant=<id>   registry with platform state (and that organisation's switches), plus organisations
    PUT  {"tenant": <id>|null, "settings": {"<vertical>" | "service:<service_type>": true|false|null}}
         tenant null -> platform default; null value -> remove the switch (inherit). Most specific wins:
         organisation+service > organisation+vertical > platform+service > platform+vertical > on.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def _deny(self, request):
        if not is_platform_superuser(request.user):
            return Response({'error': 'Platform administrators only.'}, status=status.HTTP_403_FORBIDDEN)
        return None

    def _tenant(self, raw):
        if raw in (None, '', 'null', 'platform'):
            return None
        try:
            return Client.objects.get(pk=int(raw))
        except (ValueError, TypeError, Client.DoesNotExist):
            raise LookupError

    def get(self, request):
        denied = self._deny(request)
        if denied:
            return denied
        try:
            tenant = self._tenant(request.query_params.get('tenant'))
        except LookupError:
            return Response({'error': 'Unknown organisation.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'tenant': {'id': tenant.id, 'name': tenant.name, 'industry': tenant.industry} if tenant else None,
            'verticals': verticals.describe(tenant.id if tenant else None),
            'tenants': list(Client.objects.order_by('name').values('id', 'name', 'industry')),
        })

    def put(self, request):
        denied = self._deny(request)
        if denied:
            return denied
        try:
            tenant = self._tenant(request.data.get('tenant'))
        except LookupError:
            return Response({'error': 'Unknown organisation.'}, status=status.HTTP_404_NOT_FOUND)
        changes = request.data.get('settings')
        if not isinstance(changes, dict) or not changes:
            return Response({'error': 'settings must map vertical keys to true, false or null.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from core.service_catalog import ALL_SERVICES
        known = set(verticals.OPTIONAL) | {verticals.SERVICE_PREFIX + s['service_type'] for s in ALL_SERVICES}
        bad = [k for k, v in changes.items() if k not in known or v not in (True, False, None)]
        if bad or len(changes) > 500:
            return Response({'error': f'Not configurable: {", ".join(map(str, bad[:10]))}. Use a vertical key or '
                                      '"service:<service_type>" with true, false or null (the core vertical as a '
                                      'whole is always on; its services can be switched individually).'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            for key, value in changes.items():
                if value is None:
                    VerticalSetting.objects.filter(tenant=tenant, key=key).delete()
                else:
                    VerticalSetting.objects.update_or_create(tenant=tenant, key=key,
                                                             defaults={'enabled': value, 'updated_by': request.user})
        verticals.invalidate()
        try:
            from custom_authentication.models import AdminAuditEvent
            AdminAuditEvent.objects.create(
                actor=request.user, client=tenant, action='verticals.updated',
                target_type='organisation' if tenant else 'platform', target_id=str(tenant.id) if tenant else '',
                target_label=tenant.name if tenant else 'Platform', details={'settings': changes},
                ip_address=request.META.get('REMOTE_ADDR') or None)
        except Exception:
            logger.warning('Could not record the vertical change in the audit log', exc_info=True)
        return Response({'tenant': tenant.id if tenant else None,
                         'verticals': verticals.describe(tenant.id if tenant else None)})
