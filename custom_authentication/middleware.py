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


class RBACMiddleware:
    """
    Exposes the client-supplied x-role / x-client-id / x-user-id headers on the
    request for logging/diagnostics only.

    NOTE: these headers are untrusted. Do NOT use them for authorization
    decisions; derive roles from the database (see
    custom_authentication.rbac.get_user_roles).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.role = request.META.get("HTTP_X_ROLE", "")
        request.client_id_header = request.META.get("HTTP_X_CLIENT_ID", "")
        request.user_id_header = request.META.get("HTTP_X_USER_ID", "")
        response = self.get_response(request)
        return response
