"""Browser login issues short-lived S256 grants, never confidential client secrets."""
import re
import secrets

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from oauth2_provider.models import Application, Grant
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import TwoFactorCode
from .tasks import send_signin_code_task
from .views import AUTH_THROTTLES


class BrowserAuthorizeView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES

    @transaction.atomic
    def post(self, request):
        from .views import _login_locked, _login_failed, _login_succeeded
        email = str(request.data.get("email") or "").strip().lower()
        password = request.data.get("password")
        challenge = str(request.data.get("code_challenge") or "")
        redirect_uri = request.data.get("redirect_uri")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
                or request.data.get("code_challenge_method") != "S256"
                or redirect_uri not in settings.SPA_REDIRECT_URIS):
            return Response({"error": "Invalid PKCE challenge or redirect URI"}, status=400)
        if _login_locked(email):
            return Response({"error": "Too many failed sign-in attempts. Try again later."}, status=429)
        user = authenticate(email=email, password=password) if email and password else None
        if user is None:
            _login_failed(email)
            return Response({"error": "Invalid email or password"}, status=401)
        from core.access import is_tenant_admin
        from .sso import enforced_for
        break_glass = False
        if enforced_for(user):
            if not is_tenant_admin(user):
                return Response({"error": "Your organisation signs in with single sign-on. Use 'Continue with SSO'.",
                                 "sso": True}, status=403)
            break_glass = True   # administrators may still sign in with a password, always with a code
        from .models import OrganisationPolicy
        org_requires_code = bool(user.client_id and OrganisationPolicy.objects.filter(
            client_id=user.client_id, require_2fa=True).exists())
        if user.is_2fa_enabled or org_requires_code or break_glass:
            code = request.data.get("2fa_code")
            if not code:
                TwoFactorCode.objects.filter(user=user, is_used=False).update(is_used=True)
                code = f"{secrets.randbelow(900000) + 100000}"
                TwoFactorCode.objects.create(user=user, code=code)
                try:
                    send_signin_code_task.delay(user.pk, code)
                except Exception:
                    return Response({"error": "The verification service is unavailable. Please try again."}, status=503)
                return Response({"requires_2fa": True})
            entry = TwoFactorCode.objects.select_for_update().filter(user=user, code=code, is_used=False).first()
            if not entry or not entry.is_valid():
                _login_failed(email)
                return Response({"error": "Invalid or expired verification code"}, status=401)
            entry.is_used = True
            entry.save(update_fields=["is_used"])
        _login_succeeded(email)
        grant = issue_browser_code(user, challenge, redirect_uri)
        response = Response({"code": grant["code"], "client_id": grant["client_id"],
                             "id": user.pk, "email": user.email,
                             "roles": list(user.groups.values_list("name", flat=True)),
                             "is_staff": user.is_staff, "is_superuser": user.is_superuser,
                             "organisation": user.client.name if user.client else None})
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


def issue_browser_code(user, challenge, redirect_uri):
    """A one-time authorisation code bound to the browser's PKCE challenge (exchanged at /o/token/)."""
    with transaction.atomic():
        # Lock the user while selecting/creating the per-user public application.
        type(user).objects.select_for_update().get(pk=user.pk)
        application, _ = Application.objects.get_or_create(
            user=user, name="Browser PKCE", client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            defaults={"redirect_uris": " ".join(settings.SPA_REDIRECT_URIS)},
        )
        application.redirect_uris = " ".join(settings.SPA_REDIRECT_URIS)
        application.save(update_fields=["redirect_uris"])
        code = secrets.token_urlsafe(48)
        Grant.objects.create(user=user, application=application, code=code,
                             expires=timezone.now() + timedelta(seconds=60), redirect_uri=redirect_uri,
                             scope="read write", code_challenge=challenge, code_challenge_method="S256")
    return {"code": code, "client_id": application.client_id}
