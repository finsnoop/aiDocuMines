"""
Administration API used by the admin console.

    GET  /api/v1/auth/admin/people/                       users (paged; search, role, status; org for platform admins)
    POST /api/v1/auth/admin/people/                       add a user (own organisation; any for platform admins)
    PATCH /api/v1/auth/admin/people/<id>/                 name, roles, active
    POST /api/v1/auth/admin/people/<id>/reset-password/   new temporary password (shown once)
    GET  /api/v1/auth/admin/organisations/                organisations (platform admins: all; others: their own)
    POST /api/v1/auth/admin/organisations/                create (platform admins)
    PATCH /api/v1/auth/admin/organisations/<id>/          rename / details
    GET  /api/v1/auth/admin/role-matrix/                  roles, descriptions, permissions, grantable by caller
    GET  /api/v1/auth/admin/audit/                        admin changes, sign-ins and settings changes (paged)
    GET/PATCH /api/v1/auth/admin/policy/                  the organisation's enforced settings

Organisation admins (Admin role) manage their own organisation only; platform
super-admins manage everything. Every change is written to AdminAuditEvent.
"""
import logging
import os
import secrets
import string
from heapq import merge

import requests
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_manage_user, is_platform_superuser, is_tenant_admin, scope_users, tenant_id, user_role_names
from custom_authentication.models import AdminAuditEvent, Client, CustomUser, OrganisationPolicy, UserActivityLog
from custom_authentication.rbac import ROLE_DEFINITIONS

logger = logging.getLogger(__name__)
PLATFORM_ROLES = {"SuperAdmin"}
MAX_PAGE = 100


# ── helpers ─────────────────────────────────────────────────────────────────

def _is_admin(user):
    return is_platform_superuser(user) or is_tenant_admin(user)


def _forbidden(msg="Only administrators can do this."):
    return Response({"error": msg}, status=status.HTTP_403_FORBIDDEN)


def _page(request, default=25):
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = max(1, min(MAX_PAGE, int(request.query_params.get("page_size", default))))
    except ValueError:
        size = default
    return page, size


def _ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def audit(request, action, target_type="", target_id="", label="", client_id=None, **details):
    try:
        AdminAuditEvent.objects.create(
            actor=request.user, client_id=client_id if client_id is not None else tenant_id(request.user),
            action=action, target_type=target_type, target_id=str(target_id), target_label=str(label)[:255],
            details=details, ip_address=_ip(request))
    except Exception:
        logger.exception("Could not write audit event %s", action)


def _grantable_roles(actor):
    names = set(ROLE_DEFINITIONS)
    return sorted(names if is_platform_superuser(actor) else names - PLATFORM_ROLES)


def _temp_password(length=14):
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length - 2)) + secrets.choice("!@#$%&*") + secrets.choice(string.digits)
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw):
            return pw


def _person(u, roles=None):
    return {
        "id": u.id, "email": u.email, "name": (u.contact_name or u.get_full_name() or u.username or u.email),
        "organisation": {"id": u.client_id, "name": u.client.name if u.client_id else None},
        "roles": roles if roles is not None else sorted(user_role_names(u)),
        "is_active": u.is_active, "last_login": u.last_login, "date_joined": u.date_joined,
        "two_factor": bool(getattr(u, "is_2fa_enabled", False) or getattr(u, "two_factor_enabled", False)),
    }


def _node_base():
    return (os.getenv("NODE_BACKEND_URL") or "http://aidocumines_node_backend:7000").rstrip("/")


def _register_sign_in(user, password, client_id, client_secret, existing_ok=True):
    """Sign-in first checks the Node service, so new users must exist there too."""
    try:
        r = requests.post(f"{_node_base()}/api/user", timeout=10, json={
            "username": (user.email.split("@")[0])[:150], "email": user.email, "password": password,
            "client_id": client_id, "client_secret": client_secret})
        return r.status_code in (200, 201) or (existing_ok and r.status_code == 409)
    except requests.RequestException as exc:
        logger.warning("Could not register %s with the sign-in service: %s", user.email, exc)
        return False


def _reregister_sign_in(user, password):
    """Give the user fresh OAuth app credentials and register them with the sign-in service."""
    from oauth2_provider.generators import generate_client_secret
    from oauth2_provider.models import Application
    secret = generate_client_secret()
    app = Application.objects.filter(user=user, authorization_grant_type=Application.GRANT_PASSWORD).order_by("id").first()
    if app is None:
        name = f"{user.client.name if user.client_id else 'aiDocuMines'} - {user.email}"
        app = Application.objects.create(user=user, name=name, client_type=Application.CLIENT_CONFIDENTIAL,
                                         authorization_grant_type=Application.GRANT_PASSWORD, client_secret=secret)
    else:
        app.client_secret = secret
        app.save(update_fields=["client_secret"])
    return _register_sign_in(user, password, app.client_id, secret, existing_ok=False)


def _sync_password(request, email, password):
    try:
        r = requests.post(f"{_node_base()}/api/updateUserPasswordByEmail", timeout=10,
                          headers={"Authorization": request.META.get("HTTP_AUTHORIZATION", "")},
                          json={"email": email, "newPassword": password})
        return r.status_code in (200, 201)
    except requests.RequestException as exc:
        logger.warning("Could not update sign-in password for %s: %s", email, exc)
        return False


def _managed(request, user_id):
    try:
        target = CustomUser.objects.select_related("client").get(id=user_id)
    except CustomUser.DoesNotExist:
        raise Http404("User not found")
    if not can_manage_user(request.user, target):
        raise Http404("User not found")
    return target


def _set_roles(actor, target, roles):
    allowed = set(_grantable_roles(actor))
    bad = [r for r in roles if r not in allowed]
    if bad:
        return f"You cannot grant: {', '.join(bad)}."
    if target.id == actor.id and "Admin" not in roles and not is_platform_superuser(actor):
        return "You cannot remove your own Admin role."
    keep = set(target.groups.exclude(name__in=ROLE_DEFINITIONS.keys()).values_list("name", flat=True))
    groups = [Group.objects.get_or_create(name=r)[0] for r in sorted(set(roles) | keep)]
    target.groups.set(groups)
    target.roles.set([g for g in groups if g.name in ROLE_DEFINITIONS])
    target.is_staff = "Admin" in roles or "SuperAdmin" in roles
    target.save(update_fields=["is_staff"])
    return None


# ── people ──────────────────────────────────────────────────────────────────

class PeopleView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return _forbidden()
        page, size = _page(request)
        qs = scope_users(CustomUser.objects.select_related("client"), request.user)
        if is_platform_superuser(request.user) and request.query_params.get("organisation"):
            qs = qs.filter(client_id=request.query_params["organisation"])
        term = (request.query_params.get("search") or "").strip()[:100]
        if term:
            qs = qs.filter(Q(email__icontains=term) | Q(contact_name__icontains=term) | Q(first_name__icontains=term)
                           | Q(last_name__icontains=term))
        state = request.query_params.get("status")
        if state in ("active", "disabled"):
            qs = qs.filter(is_active=(state == "active"))
        role = request.query_params.get("role")
        if role:
            qs = qs.filter(Q(groups__name=role) | Q(roles__name=role)).distinct() if hasattr(CustomUser, "roles") \
                else qs.filter(groups__name=role)
        total = qs.count()
        rows = list(qs.prefetch_related("groups").order_by("email")[(page - 1) * size: page * size])
        items = [_person(u, sorted({g.name for g in u.groups.all()} & set(ROLE_DEFINITIONS)) or sorted(user_role_names(u)))
                 for u in rows]
        return Response({"results": items, "count": total, "page": page, "page_size": size,
                         "total_pages": max(1, -(-total // size)), "grantable_roles": _grantable_roles(request.user)})

    def post(self, request):
        from oauth2_provider.models import Application
        from oauth2_provider.generators import generate_client_secret

        actor = request.user
        if not _is_admin(actor):
            return _forbidden()
        email = str(request.data.get("email") or "").strip().lower()
        name = str(request.data.get("name") or "").strip()[:150]
        roles = request.data.get("roles") or ["Client"]
        if "@" not in email or len(email) > 254:
            return Response({"error": "Enter a valid email address."}, status=400)
        if not isinstance(roles, list) or not roles:
            return Response({"error": "Choose at least one role."}, status=400)
        if is_platform_superuser(actor) and request.data.get("organisation_id"):
            client = Client.objects.filter(id=request.data["organisation_id"]).first()
        else:
            client = actor.client
        if client is None:
            return Response({"error": "Choose an organisation."}, status=400)
        if CustomUser.objects.filter(email__iexact=email).exists():
            return Response({"error": "Someone with this email already has an account."}, status=409)

        password = _temp_password()
        with transaction.atomic():
            user = CustomUser.objects.create_user(email=email, password=password, client=client)
            user.contact_name = name
            user.username = email.split("@")[0][:150]
            try:
                user.save()
            except IntegrityError:
                user.username = f"{email.split('@')[0][:140]}-{user.id}"
                user.save()
            err = _set_roles(actor, user, roles)
            if err:
                transaction.set_rollback(True)
                return Response({"error": err}, status=400)
            secret = generate_client_secret()
            app = Application.objects.create(
                user=user, name=f"{client.name} - {email}", client_type=Application.CLIENT_CONFIDENTIAL,
                authorization_grant_type=Application.GRANT_PASSWORD, client_secret=secret)

        sign_in_ready = _register_sign_in(user, password, app.client_id, secret)
        try:
            from custom_authentication.tasks import send_signup_email_task
            send_signup_email_task.delay(user.id, password, app.client_id, secret)
            invited = True
        except Exception:
            invited = False
        audit(request, "user.created", "user", user.id, email, client_id=client.id, roles=roles)
        return Response({**_person(user), "temporary_password": password, "invite_sent": invited,
                         "sign_in_ready": sign_in_ready}, status=201)


class PersonView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, user_id):
        if not _is_admin(request.user):
            return _forbidden()
        target = _managed(request, user_id)
        changes = {}
        if "name" in request.data:
            target.contact_name = str(request.data.get("name") or "")[:150]
            target.save(update_fields=["contact_name"])
            changes["name"] = target.contact_name
        if "roles" in request.data:
            roles = request.data.get("roles")
            if not isinstance(roles, list) or not roles:
                return Response({"error": "Choose at least one role."}, status=400)
            err = _set_roles(request.user, target, roles)
            if err:
                return Response({"error": err}, status=400)
            changes["roles"] = roles
        if "is_active" in request.data:
            active = bool(request.data.get("is_active"))
            if target.id == request.user.id and not active:
                return Response({"error": "You cannot disable your own account."}, status=400)
            target.is_active = active
            target.save(update_fields=["is_active"])
            changes["is_active"] = active
            if not active:
                from oauth2_provider.models import AccessToken
                AccessToken.objects.filter(user=target).delete()   # sign them out everywhere
        if changes:
            audit(request, "user.updated", "user", target.id, target.email, client_id=target.client_id, **changes)
        return Response(_person(target))


class ResetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, user_id):
        if not _is_admin(request.user):
            return _forbidden()
        target = _managed(request, user_id)
        password = _temp_password()
        target.set_password(password)
        target.save(update_fields=["password"])
        synced = _sync_password(request, target.email, password)
        if not synced:
            # The sign-in service may not know this person (e.g. they were added while it was
            # unreachable). Register them there with fresh app credentials so they can sign in.
            synced = _reregister_sign_in(target, password)
        audit(request, "user.password_reset", "user", target.id, target.email, client_id=target.client_id,
              sign_in_updated=synced)
        return Response({"temporary_password": password, "sign_in_updated": synced})


# ── organisations ───────────────────────────────────────────────────────────

def _org(c, counts=None):
    return {"id": c.id, "name": c.name, "industry": c.industry, "address": c.address or "", "created_at": c.created_at,
            "users": (counts or {}).get(c.id, 0)}


class OrganisationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return _forbidden()
        page, size = _page(request)
        qs = Client.objects.all() if is_platform_superuser(request.user) else Client.objects.filter(id=tenant_id(request.user))
        term = (request.query_params.get("search") or "").strip()[:100]
        if term:
            qs = qs.filter(name__icontains=term)
        total = qs.count()
        rows = list(qs.order_by("name")[(page - 1) * size: page * size])
        counts = dict(CustomUser.objects.filter(client_id__in=[c.id for c in rows]).values_list("client_id")
                      .annotate(n=Count("id")))
        return Response({"results": [_org(c, counts) for c in rows], "count": total, "page": page,
                         "total_pages": max(1, -(-total // size)), "can_create": is_platform_superuser(request.user)})

    def post(self, request):
        if not is_platform_superuser(request.user):
            return _forbidden("Only platform administrators can create organisations.")
        name = " ".join(str(request.data.get("name") or "").split())[:255]
        if not name:
            return Response({"error": "Enter the organisation's name."}, status=400)
        if Client.objects.filter(name__iexact=name).exists():
            return Response({"error": "An organisation with this name already exists."}, status=409)
        c = Client.objects.create(name=name, industry=str(request.data.get("industry") or "")[:255],
                                  address=str(request.data.get("address") or ""))
        OrganisationPolicy.objects.get_or_create(client=c)
        audit(request, "organisation.created", "organisation", c.id, c.name, client_id=c.id)
        return Response(_org(c), status=201)


class OrganisationView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, org_id):
        if not (is_platform_superuser(request.user) or (is_tenant_admin(request.user) and tenant_id(request.user) == org_id)):
            raise Http404("Organisation not found")
        c = Client.objects.filter(id=org_id).first()
        if not c:
            raise Http404("Organisation not found")
        if "name" in request.data:
            name = " ".join(str(request.data.get("name") or "").split())[:255]
            if not name:
                return Response({"error": "Enter the organisation's name."}, status=400)
            if Client.objects.filter(name__iexact=name).exclude(id=c.id).exists():
                return Response({"error": "An organisation with this name already exists."}, status=409)
            c.name = name
        for f in ("industry", "address"):
            if f in request.data:
                setattr(c, f, str(request.data.get(f) or "")[:255 if f == "industry" else 2000])
        c.save()
        audit(request, "organisation.updated", "organisation", c.id, c.name, client_id=c.id)
        return Response(_org(c))


# ── roles ───────────────────────────────────────────────────────────────────

class RoleMatrixView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return _forbidden()
        from custom_authentication.rbac import ROLE_DEFINITIONS as RD

        def perms(name, seen=None):
            seen = seen or set()
            if name in seen or name not in RD:
                return set()
            seen.add(name)
            d = RD[name]
            out = set(getattr(d, "permissions", []) or [])
            for parent in getattr(d, "inherits_from", []) or []:
                out |= perms(parent, seen)
            return out

        roles = [{"name": n, "label": getattr(d, "display_name", n) or n, "industry": getattr(d, "industry", None),
                  "permissions": sorted(perms(n))} for n, d in RD.items()]
        all_perms = sorted({p for r in roles for p in r["permissions"]})
        counts = dict(scope_users(CustomUser.objects.all(), request.user).values_list("groups__name")
                      .annotate(n=Count("id")))
        for r in roles:
            r["users"] = counts.get(r["name"], 0)
        return Response({"roles": roles, "permissions": all_perms, "grantable": _grantable_roles(request.user)})


# ── audit ───────────────────────────────────────────────────────────────────

class AuditView(APIView):
    """Admin changes, sign-ins and assistant use in the organisation (platform admins: everything), newest first."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return _forbidden()
        page, size = _page(request, 50)
        need = page * size
        kind = request.query_params.get("type", "all")      # all | admin | sign_in | assistant
        term = (request.query_params.get("search") or "").strip()[:100]
        platform = is_platform_superuser(request.user)
        tid = tenant_id(request.user)

        streams, total = [], 0
        if kind in ("all", "admin"):
            qs = AdminAuditEvent.objects.select_related("actor")
            if not platform:
                qs = qs.filter(client_id=tid)
            if term:
                qs = qs.filter(Q(target_label__icontains=term) | Q(action__icontains=term) | Q(actor__email__icontains=term))
            total += qs.count()
            streams.append([{"at": e.created_at, "type": "admin", "action": e.action,
                             "actor": e.actor.email if e.actor else "system", "target": e.target_label,
                             "details": e.details, "ip": e.ip_address} for e in qs.order_by("-created_at")[:need]])
        events = {"sign_in": ["LOGIN", "LOGOUT", "PASSWORD_RESET"], "assistant": ["CHAT_QUERY"]}
        if kind in ("all", "sign_in", "assistant"):
            wanted = events.get(kind, events["sign_in"] + events["assistant"])
            qs = UserActivityLog.objects.select_related("user").filter(event__in=wanted)
            if not platform:
                qs = qs.filter(user__client_id=tid)
            if term:
                qs = qs.filter(user__email__icontains=term)
            total += qs.count()
            streams.append([{"at": a.timestamp, "type": "assistant" if a.event == "CHAT_QUERY" else "sign_in",
                             "action": a.event.lower(), "actor": a.user.email, "target": (a.metadata or {}).get("context_label", ""),
                             "details": {k: v for k, v in (a.metadata or {}).items() if k != "query_preview"},
                             "ip": (a.metadata or {}).get("ip")} for a in qs.order_by("-timestamp")[:need]])
        rows = list(merge(*streams, key=lambda r: r["at"], reverse=True))[(page - 1) * size: need]
        return Response({"results": rows, "count": total, "page": page, "page_size": size,
                         "total_pages": max(1, -(-total // size))})


# ── policy ──────────────────────────────────────────────────────────────────

POLICY_FIELDS = {
    "allowed_file_types": str, "max_upload_mb": int, "assistant_enabled": bool, "assistant_web_enabled": bool,
    "default_ai_model": str, "session_timeout_minutes": int,
    "ai_provider": str, "allow_personal_ai_keys": bool,
}
AI_PROVIDERS = ("", "litellm", "openai", "ollama", "vllm")


def _policy_out(p):
    key = p.ai_api_key or ""
    return {f: getattr(p, f) for f in POLICY_FIELDS} | {
        "updated_at": p.updated_at, "updated_by": p.updated_by.email if p.updated_by else None,
        # The key itself is never returned: only whether one is set and its last characters.
        "ai_key_configured": bool(key), "ai_key_hint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else ""),
    }


class PolicyView(APIView):
    permission_classes = [IsAuthenticated]

    def _target(self, request):
        org = request.query_params.get("organisation") if is_platform_superuser(request.user) else None
        client_id = int(org) if org and str(org).isdigit() else tenant_id(request.user)
        if not client_id:
            raise Http404("No organisation")
        return OrganisationPolicy.objects.get_or_create(client_id=client_id)[0]

    def get(self, request):
        if not _is_admin(request.user):
            return _forbidden()
        return Response(_policy_out(self._target(request)))

    def patch(self, request):
        from custom_authentication.policies import forget_policy

        if not _is_admin(request.user):
            return _forbidden()
        p = self._target(request)
        changed = {}
        for f, typ in POLICY_FIELDS.items():
            if f not in request.data:
                continue
            v = request.data[f]
            try:
                v = (str(v).strip()[:500] if typ is str else bool(v) if typ is bool else int(v))
            except (TypeError, ValueError):
                return Response({"error": f"{f} has an invalid value."}, status=400)
            if f == "max_upload_mb" and not 1 <= v <= 5000:
                return Response({"error": "Upload limit must be between 1 and 5000 MB."}, status=400)
            if f == "session_timeout_minutes" and not 5 <= v <= 1440:
                return Response({"error": "Session timeout must be between 5 minutes and 24 hours."}, status=400)
            if f == "allowed_file_types":
                v = ",".join(sorted({e.strip().lstrip(".").lower() for e in v.split(",") if e.strip()}))
            if f == "ai_provider" and v not in AI_PROVIDERS:
                return Response({"error": "Choose LiteLLM, OpenAI, Ollama or vLLM."}, status=400)
            if getattr(p, f) != v:
                changed[f] = {"from": getattr(p, f), "to": v}
                setattr(p, f, v)
        # Write-only organisation AI key: a new value replaces it, clear_ai_api_key removes it.
        new_key = str(request.data.get("ai_api_key") or "").strip()
        if new_key:
            if len(new_key) < 8 or len(new_key) > 500 or any(c.isspace() for c in new_key):
                return Response({"error": "That does not look like an API key."}, status=400)
            p.ai_api_key = new_key
            changed["ai_api_key"] = "replaced"
        elif request.data.get("clear_ai_api_key") and p.ai_api_key:
            p.ai_api_key = ""
            changed["ai_api_key"] = "removed"
        if changed:
            p.updated_by = request.user
            p.save()
            forget_policy(p.client_id)
            audit(request, "settings.updated", "organisation", p.client_id, p.client.name, client_id=p.client_id,
                  changes=changed)
        return Response(_policy_out(p))
