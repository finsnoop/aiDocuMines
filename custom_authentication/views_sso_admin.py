"""
Single sign-on and SCIM settings for an organisation (its administrators; platform superusers with ?organisation=).

GET   /api/v1/auth/admin/sso/                 settings (the client secret is never returned)
PATCH /api/v1/auth/admin/sso/                 {enabled, issuer_url, client_id, client_secret?, email_domains,
                                                enforce_sso, auto_provision, default_role, scim_enabled}
POST  /api/v1/auth/admin/sso/scim-token/      a new SCIM token, shown once (the previous one stops working)
POST  /api/v1/auth/admin/sso/test/            reach the provider's OpenID configuration
"""
import re
import secrets

from django.http import Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id
from .models import OrganisationSSO
from .views_admin import audit

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def _target(request):
    if not (is_platform_superuser(request.user) or is_tenant_admin(request.user)):
        return None
    org = request.query_params.get("organisation") if is_platform_superuser(request.user) else None
    client_id = int(org) if org and str(org).isdigit() else tenant_id(request.user)
    if not client_id:
        raise Http404("No organisation")
    return OrganisationSSO.objects.get_or_create(client_id=client_id)[0]


def _out(cfg, request):
    from .sso import callback_url
    return {"enabled": cfg.enabled, "issuer_url": cfg.issuer_url, "client_id": cfg.oidc_client_id,
            "client_secret_configured": bool(cfg.client_secret), "email_domains": cfg.email_domains,
            "enforce_sso": cfg.enforce_sso, "auto_provision": cfg.auto_provision, "default_role": cfg.default_role,
            "scim_enabled": cfg.scim_enabled, "scim_token_configured": bool(cfg.scim_token_hash),
            "callback_url": callback_url(request),
            "scim_base_url": request.build_absolute_uri("/api/v1/scim/v2/").rstrip("/"),
            "updated_at": cfg.updated_at, "updated_by": cfg.updated_by.email if cfg.updated_by else None}


def _forbidden():
    return Response({"error": "Only administrators can change single sign-on."}, status=403)


class SSOSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cfg = _target(request)
        return _forbidden() if cfg is None else Response(_out(cfg, request))

    def patch(self, request):
        cfg = _target(request)
        if cfg is None:
            return _forbidden()
        data, changed = request.data, {}
        for f in ("enabled", "enforce_sso", "auto_provision", "scim_enabled"):
            if f in data:
                v = data[f]
                v = v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)
                if getattr(cfg, f) != v:
                    changed[f] = v
                    setattr(cfg, f, v)
        if "issuer_url" in data:
            url = str(data["issuer_url"] or "").strip().rstrip("/")[:500]
            if url and not url.startswith("https://"):
                return Response({"error": "The identity provider address must start with https://"}, status=400)
            cfg.issuer_url, changed["issuer_url"] = url, url
        if "client_id" in data:
            cfg.oidc_client_id = str(data["client_id"] or "").strip()[:255]
            changed["client_id"] = "changed"
        if str(data.get("client_secret") or "").strip():
            cfg.client_secret = str(data["client_secret"]).strip()[:1000]
            changed["client_secret"] = "replaced"
        if "email_domains" in data:
            domains = sorted({d.strip().lower().lstrip("@") for d in str(data["email_domains"] or "").split(",") if d.strip()})
            if any(not DOMAIN_RE.match(d) for d in domains):
                return Response({"error": "Enter email domains such as firm.com, separated by commas."}, status=400)
            # A domain signs in to exactly one organisation.
            for other in OrganisationSSO.objects.exclude(pk=cfg.pk).exclude(email_domains=""):
                taken = set(domains) & set(other.domains())
                if taken:
                    return Response({"error": f"{', '.join(sorted(taken))} is already used by another organisation."}, status=400)
            cfg.email_domains, changed["email_domains"] = ",".join(domains), domains
        if "default_role" in data:
            from .rbac import ROLE_DEFINITIONS
            role = str(data["default_role"] or "Client")
            if role not in ROLE_DEFINITIONS or role in ("SuperAdmin", "Admin"):
                return Response({"error": "Choose a standard role for new accounts (not an administrator role)."}, status=400)
            cfg.default_role, changed["default_role"] = role, role
        if cfg.enabled and not (cfg.issuer_url and cfg.oidc_client_id and cfg.client_secret and cfg.domains()):
            return Response({"error": "Fill in the provider address, client ID, client secret and email domains before "
                                      "turning single sign-on on."}, status=400)
        cfg.updated_by = request.user
        cfg.save()
        audit(request, "sso.update", "organisation", cfg.client_id, client_id=cfg.client_id, changes=changed)
        return Response(_out(cfg, request))


class SCIMTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .scim import hash_token
        cfg = _target(request)
        if cfg is None:
            return _forbidden()
        token = "scim_" + secrets.token_urlsafe(40)
        cfg.scim_token_hash, cfg.scim_enabled, cfg.updated_by = hash_token(token), True, request.user
        cfg.save(update_fields=["scim_token_hash", "scim_enabled", "updated_by", "updated_at"])
        audit(request, "scim.token_issued", "organisation", cfg.client_id, client_id=cfg.client_id)
        return Response({"token": token, "note": "Copy this token now; it is not shown again."})


class SSOTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .sso import SSOError, discovery
        cfg = _target(request)
        if cfg is None:
            return _forbidden()
        try:
            meta = discovery(cfg)
        except SSOError as exc:
            return Response({"ok": False, "error": str(exc)}, status=200)
        return Response({"ok": True, "issuer": meta.get("issuer")})
