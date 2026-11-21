"""
Single sign-on with an organisation's identity provider (OpenID Connect: Entra ID, Okta, Google, ...).

    POST /api/v1/auth/sso/discover/  {email}                       does this email sign in with SSO?
    POST /api/v1/auth/sso/start/     {email, code_challenge, code_challenge_method, redirect_uri}
                                                                    -> {authorize_url} (the browser goes there)
    GET  /api/v1/auth/sso/callback/  ?code&state                    the provider returns here; we redirect to
                                                                    the app with our own one-time code

Security:
* The browser's own PKCE challenge is kept server-side; the final step is the same one-time code exchange
  as password sign-in (/o/token/ with the browser's verifier), so tokens never travel in a URL.
* Our leg to the provider uses its own PKCE, a single-use state (10 minutes) and a nonce.
* The ID token is verified: signature (provider's published keys), issuer, audience, expiry and nonce.
* The email must be verified (when the provider says), belong to one of the organisation's domains, and an
  existing account must belong to that organisation; a matching account elsewhere is never signed in.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseRedirect
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CustomUser, OrganisationSSO

logger = logging.getLogger(__name__)
STATE_TTL = 600
DISCOVERY_TTL = 3600


class SSOError(Exception):
    """A failure that is safe to show to the person signing in."""


# ── configuration lookups ────────────────────────────────────────────────────────────────────
def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def config_for_email(email: str):
    """The enabled SSO configuration whose domains include this email, or None."""
    domain = domain_of(email)
    if not domain:
        return None
    for cfg in OrganisationSSO.objects.filter(enabled=True).select_related("client"):
        if domain in cfg.domains():
            return cfg
    return None


def enforced_for(user) -> bool:
    """Must this person sign in with SSO (password sign-in refused)?"""
    cfg = OrganisationSSO.objects.filter(client_id=user.client_id).first() if getattr(user, "client_id", None) else None
    return bool(cfg and cfg.enabled and cfg.enforce_sso and domain_of(user.email) in cfg.domains())


def callback_url(request) -> str:
    base = (os.getenv("SSO_PUBLIC_API_URL") or os.getenv("INTEGRATIONS_PUBLIC_API_URL") or "").strip().rstrip("/")
    return f"{base or request.build_absolute_uri('/').rstrip('/')}/api/v1/auth/sso/callback/"


def discovery(cfg) -> dict:
    """The provider's OpenID configuration (cached)."""
    issuer = cfg.issuer_url.rstrip("/")
    if not issuer.startswith("https://"):
        raise SSOError("The identity provider address must use https.")
    key = "sso-discovery:" + hashlib.sha256(issuer.encode()).hexdigest()
    data = cache.get(key)
    if data is None:
        try:
            response = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=8)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise SSOError("The identity provider could not be reached. Try again or contact your administrator.") from exc
        if not data.get("issuer") or any(not str(data.get(f, "")).startswith("https://")
                                         for f in ("authorization_endpoint", "token_endpoint", "jwks_uri")):
            raise SSOError("The identity provider's configuration is incomplete.")
        cache.set(key, data, DISCOVERY_TTL)
    return data


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def verify_id_token(cfg, meta, id_token, nonce) -> dict:
    import jwt
    try:
        signing_key = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True, timeout=8).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, signing_key.key, algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                            audience=cfg.oidc_client_id, issuer=meta["issuer"], options={"require": ["exp", "iat", "aud", "iss"]})
    except Exception as exc:
        raise SSOError("The identity provider's answer could not be verified.") from exc
    if claims.get("nonce") != nonce:
        raise SSOError("The sign-in response did not match this request. Please start again.")
    return claims


def email_from(claims) -> str:
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    if claims.get("email_verified") is False:
        raise SSOError("Your identity provider has not verified your email address.")
    email = str(email).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise SSOError("Your identity provider did not send an email address.")
    return email


@transaction.atomic
def user_for(cfg, email) -> CustomUser:
    if domain_of(email) not in cfg.domains():
        raise SSOError("This email address is not part of your organisation's sign-in domains.")
    user = CustomUser.objects.select_for_update().filter(email__iexact=email).first()
    if user is not None:
        if user.client_id != cfg.client_id:
            logger.warning("SSO refused: %s belongs to another organisation", email)
            raise SSOError("This account belongs to a different organisation.")
        if not user.is_active:
            raise SSOError("Your account is disabled. Contact your administrator.")
        return user
    if not cfg.auto_provision:
        raise SSOError("You do not have an account yet. Ask your administrator to add you.")
    from django.contrib.auth.models import Group
    from custom_authentication.rbac import ROLE_DEFINITIONS
    user = CustomUser.objects.create_user(email=email, client=cfg.client, password=None)
    user.set_unusable_password()
    user.username = _unique_username(email)
    user.save(update_fields=["password", "username"])
    role = cfg.default_role if cfg.default_role in ROLE_DEFINITIONS and cfg.default_role not in ("SuperAdmin",) else "Client"
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    logger.info("SSO provisioned user %s in organisation %s with role %s", user.pk, cfg.client_id, role)
    return user


def _unique_username(email):
    base = re.sub(r"[^a-zA-Z0-9_.-]", "", email.split("@")[0])[:120] or "user"
    name, n = base, 1
    while CustomUser.objects.filter(username=name).exists():
        n += 1
        name = f"{base}{n}"
    return name


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
def _throttles():
    from .views import AUTH_THROTTLES
    return AUTH_THROTTLES


class SSODiscoverView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [t() for t in _throttles()]

    def post(self, request):
        cfg = config_for_email(str(request.data.get("email") or ""))
        return Response({"sso": bool(cfg), "enforced": bool(cfg and cfg.enforce_sso),
                         "organisation": cfg.client.name if cfg else None})


class SSOStartView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [t() for t in _throttles()]

    def post(self, request):
        email = str(request.data.get("email") or "").strip().lower()
        challenge = str(request.data.get("code_challenge") or "")
        redirect_uri = request.data.get("redirect_uri")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) or request.data.get("code_challenge_method") != "S256"
                or redirect_uri not in settings.SPA_REDIRECT_URIS):
            return Response({"error": "Invalid PKCE challenge or redirect URI"}, status=400)
        cfg = config_for_email(email)
        if cfg is None:
            return Response({"error": "Single sign-on is not set up for this email address."}, status=404)
        try:
            meta = discovery(cfg)
        except SSOError as exc:
            return Response({"error": str(exc)}, status=502)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, idp_challenge = _pkce()
        cache.set(f"sso-state:{state}", {"org": cfg.client_id, "challenge": challenge, "redirect_uri": redirect_uri,
                                         "nonce": nonce, "verifier": verifier, "callback": callback_url(request)}, STATE_TTL)
        params = {"response_type": "code", "client_id": cfg.oidc_client_id, "redirect_uri": callback_url(request),
                  "scope": "openid email profile", "state": state, "nonce": nonce, "login_hint": email,
                  "code_challenge": idp_challenge, "code_challenge_method": "S256"}
        return Response({"authorize_url": f"{meta['authorization_endpoint']}?{urlencode(params)}"})


class SSOCallbackView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        return [t() for t in _throttles()]

    def get(self, request):
        state = str(request.query_params.get("state") or "")
        saved = cache.get(f"sso-state:{state}") if state else None
        if saved:
            cache.delete(f"sso-state:{state}")   # single use
        if not saved:
            return _back(settings.SPA_REDIRECT_URIS[0] if settings.SPA_REDIRECT_URIS else "/",
                         error="This sign-in link has expired. Please start again.")
        redirect_uri = saved["redirect_uri"]
        if request.query_params.get("error"):
            return _back(redirect_uri, error="Sign-in was cancelled or refused by your identity provider.")
        try:
            cfg = OrganisationSSO.objects.select_related("client").get(client_id=saved["org"], enabled=True)
            meta = discovery(cfg)
            token = requests.post(meta["token_endpoint"], timeout=10, data={
                "grant_type": "authorization_code", "code": str(request.query_params.get("code") or ""),
                "redirect_uri": saved["callback"], "client_id": cfg.oidc_client_id, "client_secret": cfg.client_secret,
                "code_verifier": saved["verifier"]})
            if token.status_code != 200 or not token.json().get("id_token"):
                raise SSOError("Your identity provider did not complete the sign-in.")
            claims = verify_id_token(cfg, meta, token.json()["id_token"], saved["nonce"])
            user = user_for(cfg, email_from(claims))
        except OrganisationSSO.DoesNotExist:
            return _back(redirect_uri, error="Single sign-on is no longer enabled for your organisation.")
        except SSOError as exc:
            return _back(redirect_uri, error=str(exc))
        except Exception:
            logger.exception("SSO callback failed")
            return _back(redirect_uri, error="Sign-in failed. Please try again.")
        from .browser_auth import issue_browser_code
        from .views import _login_succeeded
        _login_succeeded(user.email)
        grant = issue_browser_code(user, saved["challenge"], redirect_uri)
        logger.info("SSO sign-in: user %s (organisation %s)", user.pk, cfg.client_id)
        return _back(redirect_uri, code=grant["code"], client_id=grant["client_id"], sso="1")


def _back(redirect_uri, **params):
    response = HttpResponseRedirect(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{urlencode(params)}")
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response
