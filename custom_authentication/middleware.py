# custom_authentication/middleware.py

from .models import UserAPICall

class APICallLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            UserAPICall.objects.create(user=request.user, endpoint=request.path)
        response = self.get_response(request)
        return response


class ClientIdentityBindingMiddleware:
    """
    SECURITY: many views resolve "the acting user" from the X-Client-ID header
    (get_user_from_client_id). A header is caller-controlled, so a logged-in user
    could send someone else's client id and act as them, in any tenant.

    This middleware must run AFTER OAuth2TokenMiddleware. When a request carries a
    bearer token AND an X-Client-ID, the client id must belong to an OAuth
    application owned by the token's user; otherwise the request is rejected.
    Requests without a token (login, signup, password reset) are untouched: those
    views check the client secret themselves.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        client_id = request.META.get("HTTP_X_CLIENT_ID", "").strip()
        user = getattr(request, "user", None)
        if client_id and user is not None and user.is_authenticated:
            from oauth2_provider.models import Application

            if not Application.objects.filter(client_id=client_id, user=user).exists():
                from django.http import JsonResponse

                return JsonResponse(
                    {"error": "X-Client-ID does not belong to the authenticated user."},
                    status=403,
                )
        return self.get_response(request)


class RBACMiddleware:
    """
    SECURITY: role, tenant and user identity are NEVER taken from request
    headers. Browsers can set any header, so trusting X-Role / X-User-ID /
    X-Client-ID allowed privilege escalation and cross-tenant access.

    `request.role` is kept (empty) for backwards compatibility; the real role is
    resolved from the authenticated user via `core.access.primary_role(user)`.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.role = ""
        return self.get_response(request)
