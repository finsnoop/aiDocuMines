"""
SCIM 2.0 user provisioning (RFC 7643/7644) for an organisation's identity provider (Entra ID, Okta, ...).

    /api/v1/scim/v2/ServiceProviderConfig
    /api/v1/scim/v2/Users            GET (filter userName eq "x", startIndex, count), POST
    /api/v1/scim/v2/Users/<id>       GET, PUT, PATCH, DELETE

Authentication: "Authorization: Bearer <SCIM token>" generated in Users & access; only its SHA-256 is stored.
A token reaches only its own organisation's users. Deactivation (active=false, or DELETE) disables the account
and revokes its sessions at once; accounts are never hard-deleted, so their documents and audit trail remain.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re

from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CustomUser, OrganisationSSO

logger = logging.getLogger(__name__)
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
MAX_COUNT = 200


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _error(status, detail, scim_type=None):
    body = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return Response(body, status=status, content_type="application/scim+json")


def _ok(body, status=200):
    return Response(body, status=status, content_type="application/scim+json")


def revoke_sessions(user):
    from oauth2_provider.models import AccessToken, RefreshToken
    RefreshToken.objects.filter(user=user).delete()
    AccessToken.objects.filter(user=user).delete()


def user_resource(u, request):
    return {"schemas": [USER_SCHEMA], "id": str(u.pk), "externalId": getattr(u, "scim_external_id", None) or None,
            "userName": u.email, "active": u.is_active,
            "name": {"givenName": u.first_name or "", "familyName": u.last_name or ""},
            "displayName": (f"{u.first_name} {u.last_name}".strip() or u.email),
            "emails": [{"value": u.email, "primary": True, "type": "work"}],
            "meta": {"resourceType": "User", "location": request.build_absolute_uri(f"/api/v1/scim/v2/Users/{u.pk}")}}


class _SCIM(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.cfg = None
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Bearer ") and len(header) > 30:
            digest = hash_token(header[7:].strip())
            for cfg in OrganisationSSO.objects.filter(scim_enabled=True).exclude(scim_token_hash="").select_related("client"):
                if hmac.compare_digest(cfg.scim_token_hash, digest):
                    self.cfg = cfg
                    break

    def denied(self):
        return _error(401, "A valid SCIM token is required.") if self.cfg is None else None

    def users(self):
        return CustomUser.objects.filter(client_id=self.cfg.client_id)

    def _apply(self, user, data):
        name = data.get("name") or {}
        if isinstance(name, dict):
            if "givenName" in name:
                user.first_name = str(name.get("givenName") or "")[:150]
            if "familyName" in name:
                user.last_name = str(name.get("familyName") or "")[:150]
        if "active" in data:
            self._set_active(user, data["active"])

    def _set_active(self, user, value):
        active = value if isinstance(value, bool) else str(value).lower() == "true"
        if user.is_active and not active:
            revoke_sessions(user)
        user.is_active = active


class ServiceProviderConfigView(_SCIM):
    def get(self, request):
        if (denied := self.denied()):
            return denied
        return _ok({"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
                    "patch": {"supported": True}, "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                    "filter": {"supported": True, "maxResults": MAX_COUNT}, "changePassword": {"supported": False},
                    "sort": {"supported": False}, "etag": {"supported": False},
                    "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token", "primary": True,
                                               "description": "SCIM token from Users & access"}]})


class UsersView(_SCIM):
    def get(self, request):
        if (denied := self.denied()):
            return denied
        qs = self.users().order_by("pk")
        flt = str(request.query_params.get("filter") or "")
        if flt:
            m = re.fullmatch(r'\s*userName\s+eq\s+"([^"]+)"\s*', flt, re.I)
            if not m:
                return _error(400, "Only 'userName eq \"...\"' filters are supported.", "invalidFilter")
            qs = qs.filter(email__iexact=m.group(1))
        try:
            start = max(int(request.query_params.get("startIndex", 1)), 1)
            count = min(max(int(request.query_params.get("count", 100)), 0), MAX_COUNT)
        except ValueError:
            return _error(400, "startIndex and count must be numbers.", "invalidValue")
        total = qs.count()
        page = list(qs[start - 1:start - 1 + count])
        return _ok({"schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": start, "itemsPerPage": len(page),
                    "Resources": [user_resource(u, request) for u in page]})

    def post(self, request):
        if (denied := self.denied()):
            return denied
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("userName") or "").strip().lower()
        if not email and isinstance(data.get("emails"), list) and data["emails"]:
            email = str(data["emails"][0].get("value") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return _error(400, "userName must be an email address.", "invalidValue")
        from .sso import _unique_username, domain_of
        if self.cfg.domains() and domain_of(email) not in self.cfg.domains():
            return _error(400, "This email is outside the organisation's sign-in domains.", "invalidValue")
        if CustomUser.objects.filter(email__iexact=email).exists():
            return _error(409, "A user with this userName already exists.", "uniqueness")
        from custom_authentication.rbac import ROLE_DEFINITIONS
        try:
            with transaction.atomic():
                user = CustomUser.objects.create_user(email=email, client=self.cfg.client, password=None)
                user.set_unusable_password()
                user.username = _unique_username(email)
                self._apply(user, data)
                user.save()
                role = self.cfg.default_role if self.cfg.default_role in ROLE_DEFINITIONS and self.cfg.default_role != "SuperAdmin" else "Client"
                user.groups.add(Group.objects.get_or_create(name=role)[0])
        except IntegrityError:
            return _error(409, "A user with this userName already exists.", "uniqueness")
        logger.info("SCIM created user %s in organisation %s", user.pk, self.cfg.client_id)
        return _ok(user_resource(user, request), status=201)


class UserDetailView(_SCIM):
    def _user(self, pk):
        return self.users().filter(pk=pk).first() if str(pk).isdigit() else None

    def get(self, request, pk):
        if (denied := self.denied()):
            return denied
        user = self._user(pk)
        return _ok(user_resource(user, request)) if user else _error(404, "User not found.")

    def put(self, request, pk):
        if (denied := self.denied()):
            return denied
        user = self._user(pk)
        if user is None:
            return _error(404, "User not found.")
        self._apply(user, request.data if isinstance(request.data, dict) else {})
        user.save()
        return _ok(user_resource(user, request))

    def patch(self, request, pk):
        if (denied := self.denied()):
            return denied
        user = self._user(pk)
        if user is None:
            return _error(404, "User not found.")
        ops = (request.data or {}).get("Operations") if isinstance(request.data, dict) else None
        if not isinstance(ops, list):
            return _error(400, "PatchOp Operations are required.", "invalidSyntax")
        for op in ops:
            kind = str(op.get("op") or "").lower()
            path = str(op.get("path") or "")
            value = op.get("value")
            if kind not in ("replace", "add"):
                continue   # remove of optional attributes is ignored
            if path.lower() == "active":
                self._set_active(user, value)
            elif path in ("name.givenName", "name.familyName"):
                self._apply(user, {"name": {path.split(".")[1]: value}})
            elif not path and isinstance(value, dict):
                self._apply(user, value)
        user.save()
        logger.info("SCIM updated user %s (active=%s)", user.pk, user.is_active)
        return _ok(user_resource(user, request))

    def delete(self, request, pk):
        if (denied := self.denied()):
            return denied
        user = self._user(pk)
        if user is None:
            return _error(404, "User not found.")
        self._set_active(user, False)   # deprovision: disable and sign out, never erase
        user.save(update_fields=["is_active"])
        logger.info("SCIM deprovisioned user %s", user.pk)
        return Response(status=204)
