"""Refuse a vertical's API when that vertical is switched off for the caller's organisation."""
from django.http import JsonResponse

from . import verticals


class VerticalAccessMiddleware:
    """Placed after OAuth2TokenMiddleware so bearer-token users are known. Unauthenticated requests pass
    through to the view, which answers 401 as before. Platform superusers keep access for support."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = verticals.vertical_for_path(request.path)
        if key:
            user = getattr(request, 'user', None)
            if getattr(user, 'is_authenticated', False):
                from core.access import is_platform_superuser
                if not is_platform_superuser(user) and key not in verticals.enabled_for(user):
                    return JsonResponse({'error': 'This service is not enabled for your organisation.',
                                         'vertical': key}, status=403)
        return self.get_response(request)
