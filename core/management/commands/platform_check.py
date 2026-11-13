"""
End-to-end health check for a deployed platform. Run inside the API container:

    python manage.py platform_check
    python manage.py platform_check --as user@firm.com --send-test-email you@firm.com --chat

Checks: database, migrations, Redis/Celery, Elasticsearch, LiteLLM/OpenAI (+ an
optional real chat answer), OnlyOffice (health, JWT secret, optional conversion
of a real file), Nextcloud (status, WebDAV login, OIDC discovery), email, and a
sweep of every parameter-free GET endpoint as a real user (any 5xx is a failure).

Read-only except: --send-test-email sends one email; --chat makes one model call;
--onlyoffice-file-id converts one file (nothing is saved).
Exit code 0 when every check passes, 1 otherwise.
"""
import os
import smtplib
import time

import jwt
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection
from django.urls import URLPattern, URLResolver, get_resolver

TIMEOUT = 15
SKIP_WORDS = (
    "delete", "remove", "purge", "clean", "reset", "trash", "restore", "revoke", "logout", "deactivate",
    "reactivate", "sync", "autologin", "redirect", "import", "export", "cancel", "retry", "run", "execute",
    "trigger", "send", "provision", "callback", "signed-download", "download", "stream", "swagger", "redoc",
    "o/", "oidc", "admin/", "webhook", "stripe", "payment", "break", "supersuperuser", "create",
)


NOT_CHAT = ("embed", "tts", "whisper", "audio", "realtime", "image", "dall-e", "moderation", "transcribe",
            "search", "instruct", "babbage", "davinci", "codex", "computer-use", "sora")


def pick_chat_model(ids):
    """A model that answers chat completions: the configured one if offered, else a sensible default."""
    chat = [i for i in ids if i and not any(w in i.lower() for w in NOT_CHAT)]
    preferred = os.getenv("OPENAI_MODEL")
    for want in (preferred, "gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"):
        if want and want in chat:
            return want
    return chat[0] if chat else None


class Result:
    def __init__(self):
        self.rows = []

    def add(self, area, ok, detail=""):
        self.rows.append((area, ok, detail))
        return ok


def _get_paths(resolver=None, prefix=""):
    """Every URL that takes no path parameters, as a concrete path."""
    resolver = resolver or get_resolver()
    for p in resolver.url_patterns:
        route = str(p.pattern)
        if "<" in route or "(?P" in route or "(" in route:
            continue  # needs parameters
        route = route.lstrip("^").rstrip("$")
        if isinstance(p, URLResolver):
            yield from _get_paths(p, prefix + route)
        elif isinstance(p, URLPattern):
            yield "/" + prefix + route


class Command(BaseCommand):
    help = "Check every service and endpoint the platform depends on."

    def add_arguments(self, parser):
        parser.add_argument("--as", dest="as_email", help="Sweep endpoints as this user (default: first superuser).")
        parser.add_argument("--send-test-email", dest="email_to", help="Send one test email to this address.")
        parser.add_argument("--chat", action="store_true", help="Ask the default model one short question.")
        parser.add_argument("--onlyoffice-file-id", type=int, help="Convert this file to PDF through OnlyOffice.")
        parser.add_argument("--skip-endpoints", action="store_true", help="Skip the GET endpoint sweep.")

    # ── checks ────────────────────────────────────────────────────────────────
    def check_database(self, r):
        try:
            with connection.cursor() as c:
                c.execute("SELECT 1")
            r.add("Database", True, f"{connection.vendor} {connection.settings_dict.get('HOST')}")
        except Exception as e:
            r.add("Database", False, str(e)[:200])
            return
        from django.db.migrations.executor import MigrationExecutor
        plan = MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes())
        r.add("Migrations", not plan, "all applied" if not plan else f"{len(plan)} pending: run manage.py migrate")

    def check_redis_celery(self, r):
        url = getattr(settings, "CELERY_BROKER_URL", "") or os.getenv("CELERY_BROKER_URL", "")
        try:
            import redis
            redis.Redis.from_url(url, socket_timeout=5).ping()
            r.add("Redis (Celery broker)", True, url.split("@")[-1])
        except Exception as e:
            r.add("Redis (Celery broker)", False, str(e)[:200])
            return
        try:
            from aiDocuMines.celery_app import app
            replies = app.control.ping(timeout=3) or []
            r.add("Celery workers", bool(replies), f"{len(replies)} worker(s) answered" if replies else "no worker answered")
        except Exception as e:
            r.add("Celery workers", False, str(e)[:200])

    def check_elasticsearch(self, r):
        try:
            from elasticsearch_dsl.connections import connections
            es = connections.get_connection()
            health = es.cluster.health()
            r.add("Elasticsearch cluster", health.get("status") in ("green", "yellow"),
                  f"status {health.get('status')}, {health.get('number_of_nodes')} node(s)")
        except Exception as e:
            return r.add("Elasticsearch cluster", False, str(e)[:200])
        try:
            if not es.indices.exists(index="files"):
                return r.add("Elasticsearch 'files' index", False, "missing: run repair_search_indexes --rebuild-es")
            st = es.indices.get_settings(index="files")
            blocks = next(iter(st.values()))["settings"]["index"].get("blocks", {})
            read_only = str(blocks.get("read_only_allow_delete", "false")).lower() == "true"
            count = es.count(index="files")["count"]
            r.add("Elasticsearch 'files' index", not read_only,
                  f"{count} documents" + (" - READ-ONLY (disk filled up): free disk, then repair_search_indexes --unblock-es"
                                          if read_only else ""))
            disk = es.cat.allocation(format="json")
            worst = max((int(str(d.get("disk.percent") or 0)) for d in disk), default=0)
            r.add("Elasticsearch disk", worst < 85, f"{worst}% used (indexing stops at 95%)")
        except Exception as e:
            r.add("Elasticsearch 'files' index", False, str(e)[:200])

    def check_milvus(self, r):
        try:
            from document_search import milvus_store
            st = milvus_store.stats()
            r.add("Milvus collection", "Loaded" in st["load_state"],
                  f"{st['entities']} vectors, {st['partitions']} partitions, {st['load_state']}")
        except Exception as e:
            return r.add("Milvus collection", False, str(e)[:200])
        try:
            from document_search.tasks import find_unindexed
            rep = find_unindexed(limit=200)
            missing_m, missing_e = len(rep["milvus_missing"]), rep["es_missing"]
            r.add("Search index coverage (newest 200 files)", missing_m == 0 and not missing_e,
                  f"{missing_m} missing from Milvus, "
                  + (f"{len(missing_e)} from Elasticsearch" if missing_e is not None else "Elasticsearch not checked")
                  + ("" if missing_m == 0 and not missing_e else " - run repair_search_indexes --fix"))
        except Exception as e:
            r.add("Search index coverage (newest 200 files)", False, str(e)[:200])

    def check_models(self, r, chat):
        from core.llm_providers import ProviderError, default_provider, list_models

        first = {}
        for provider in ("litellm", "openai"):
            if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
                r.add("Models: openai", True, "no key configured (skipped)")
                continue
            try:
                data = list_models(provider)
                ids = [m.get("id") for m in data.get("data", [])]
                first[provider] = pick_chat_model(ids)
                r.add(f"Models: {provider}", bool(ids), f"{len(ids)} models, e.g. {', '.join(ids[:4])}")
            except ProviderError as e:
                r.add(f"Models: {provider}", False, str(e))
        if chat:
            from grid_documents_interrogation.utils import dispatch_to_llm

            provider = default_provider()
            model = first.get(provider) or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            try:
                t = time.time()
                answer = dispatch_to_llm("What is the notice period? Answer in five words.",
                                         "Either party may terminate on 30 days written notice.",
                                         {"provider": provider, "model": model})
                r.add(f"Chat answer ({provider}/{model})", bool(answer), f"{time.time() - t:.1f}s: {answer[:80]!r}")
            except Exception as e:
                r.add(f"Chat answer ({provider}/{model})", False, str(e)[:200])

    def check_onlyoffice(self, r, file_id):
        oo = getattr(settings, "ONLYOFFICE", {}) or {}
        base = (oo.get("DOC_SERVER_URL") or "").rstrip("/")
        secret = oo.get("JWT_SECRET") or ""
        if not base:
            return r.add("OnlyOffice", False, "ONLYOFFICE_URL not set")
        try:
            resp = requests.get(f"{base}/healthcheck", timeout=TIMEOUT)
            r.add("OnlyOffice health", resp.ok and "true" in resp.text.lower(), f"{base} -> {resp.text.strip()[:30]}")
        except Exception as e:
            return r.add("OnlyOffice health", False, f"{base}: {str(e)[:150]}")
        if not secret:
            return r.add("OnlyOffice JWT secret", False, "ONLYOFFICE_JWT_SECRET not set: callbacks cannot be verified")

        # A signed conversion of an unreachable URL: the server answers -4 (download
        # failed) when it accepts our signature, -8 when the secret is wrong.
        body = {"async": False, "filetype": "docx", "outputtype": "pdf", "key": f"check-{int(time.time())}",
                "title": "check.docx", "url": "http://invalid.invalid/check.docx"}
        signed = dict(body, token=jwt.encode(body, secret, algorithm="HS256"))
        try:
            resp = requests.post(f"{base}/ConvertService.ashx", json=signed, timeout=TIMEOUT,
                                 headers={"Accept": "application/json"})
            code = (resp.json() or {}).get("error")
            r.add("OnlyOffice JWT secret", code != -8,
                  "secret accepted" if code != -8 else "Document Server rejected the signature (-8): secrets differ")
        except Exception as e:
            r.add("OnlyOffice JWT secret", False, str(e)[:200])

        api = getattr(settings, "API_BASE_URL", "").rstrip("/")
        try:
            resp = requests.get(f"{api}/api/v1/health/", timeout=TIMEOUT)
            r.add("API reachable at API_BASE_URL", resp.ok, f"{api} HTTP {resp.status_code} (OnlyOffice downloads/saves use it)")
        except Exception as e:
            r.add("API reachable at API_BASE_URL", False, f"{api}: {str(e)[:150]}")

        if file_id:
            from core.models import File
            from core.onlyoffice_utils import ext_from_filename, make_signed_download_url

            f = File.objects.filter(pk=file_id).first()
            if not f:
                return r.add("OnlyOffice conversion", False, f"file {file_id} not found")
            body = {"async": False, "filetype": ext_from_filename(f.filename), "outputtype": "pdf",
                    "key": f"check-{f.id}-{int(time.time())}", "title": f.filename,
                    "url": make_signed_download_url(str(f.id))}
            signed = dict(body, token=jwt.encode(body, secret, algorithm="HS256"))
            try:
                resp = requests.post(f"{base}/ConvertService.ashx", json=signed, timeout=120,
                                     headers={"Accept": "application/json"})
                data = resp.json() or {}
                ok = bool(data.get("fileUrl")) and data.get("endConvert")
                r.add("OnlyOffice conversion", ok, f"{f.filename} -> PDF" if ok else f"response {data}")
            except Exception as e:
                r.add("OnlyOffice conversion", False, str(e)[:200])

    def check_nextcloud(self, r):
        base = (os.getenv("NEXTCLOUD_URL") or os.getenv("NEXTCLOUD_BASE_URL") or "").rstrip("/")
        if not base:
            return r.add("Nextcloud", True, "NEXTCLOUD_URL not set (skipped)")
        try:
            st = requests.get(f"{base}/status.php", timeout=TIMEOUT).json()
            ok = st.get("installed") and not st.get("maintenance") and not st.get("needsDbUpgrade")
            r.add("Nextcloud status", bool(ok), f"version {st.get('versionstring')}, maintenance={st.get('maintenance')}")
        except Exception as e:
            return r.add("Nextcloud status", False, f"{base}: {str(e)[:150]}")
        user, pw = os.getenv("NEXTCLOUD_ADMIN_USER"), os.getenv("NEXTCLOUD_ADMIN_PASS")
        if user and pw:
            try:
                resp = requests.request("PROPFIND", f"{base}/remote.php/dav/files/{user}/", auth=(user, pw),
                                        headers={"Depth": "0"}, timeout=TIMEOUT)
                r.add("Nextcloud admin login (WebDAV)", resp.status_code == 207, f"HTTP {resp.status_code}")
            except Exception as e:
                r.add("Nextcloud admin login (WebDAV)", False, str(e)[:150])
        api = getattr(settings, "API_BASE_URL", "").rstrip("/")
        try:
            resp = requests.get(f"{api}/o/.well-known/openid-configuration", timeout=TIMEOUT)
            issuer = resp.json().get("issuer") if resp.ok else None
            r.add("OIDC discovery for Nextcloud sign-in", bool(issuer), f"issuer {issuer}" if issuer else f"HTTP {resp.status_code}")
        except Exception as e:
            r.add("OIDC discovery for Nextcloud sign-in", False, str(e)[:150])

    def check_email(self, r, to):
        host, port = getattr(settings, "EMAIL_HOST", ""), int(getattr(settings, "EMAIL_PORT", 587) or 587)
        if not host or "smtp" not in getattr(settings, "EMAIL_BACKEND", "smtp"):
            return r.add("Email (SMTP)", True, "SMTP not configured (skipped)")
        try:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            if getattr(settings, "EMAIL_USE_TLS", False):
                s.starttls()
            if getattr(settings, "EMAIL_HOST_USER", ""):
                s.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
            s.quit()
            r.add("Email (SMTP login)", True, f"{host}:{port}")
        except Exception as e:
            return r.add("Email (SMTP login)", False, str(e)[:200])
        if to:
            from django.core.mail import send_mail
            try:
                send_mail("aiDocuMines platform check", "This is a test message from manage.py platform_check.",
                          settings.DEFAULT_FROM_EMAIL, [to])
                r.add("Email (test message sent)", True, to)
            except Exception as e:
                r.add("Email (test message sent)", False, str(e)[:200])

    def sweep_endpoints(self, r, as_email):
        from rest_framework.test import APIClient

        User = get_user_model()
        user = User.objects.filter(email=as_email).first() if as_email else \
            User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
        if not user:
            return r.add("Endpoint sweep", False, f"user {as_email or '(superuser)'} not found")
        # A real, short-lived OAuth token so permission checks behave exactly as in production.
        from datetime import timedelta
        from django.utils import timezone
        from oauth2_provider.models import AccessToken, Application
        import secrets

        app = Application.objects.filter(user=user).first()
        temp_app = None
        if app is None:
            temp_app = app = Application.objects.create(
                user=user, name="platform-check", client_type=Application.CLIENT_CONFIDENTIAL,
                authorization_grant_type=Application.GRANT_PASSWORD)
        token = AccessToken.objects.create(user=user, application=app, token=secrets.token_urlsafe(32),
                                           expires=timezone.now() + timedelta(minutes=10), scope="read write")
        host = next((h for h in settings.ALLOWED_HOSTS if h and h != "*" and not h.startswith(".")), "localhost")
        client = APIClient(SERVER_NAME=host)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token.token}", "HTTP_X_CLIENT_ID": app.client_id}

        paths = sorted({p for p in _get_paths() if p.startswith("/api/") and not any(w in p.lower() for w in SKIP_WORDS)})
        failures, counts, denied = [], {}, []
        for path in paths:
            try:
                resp = client.get(path, **headers)
                code = resp.status_code
            except Exception as e:
                code, resp = 599, None
                failures.append(f"{path} -> exception {type(e).__name__}: {str(e)[:80]}")
            counts[code // 100] = counts.get(code // 100, 0) + 1
            if code in (401, 403):
                denied.append(path)
            if code >= 500 and resp is not None:
                failures.append(f"{path} -> HTTP {code}")
        token.delete()
        if temp_app is not None:
            temp_app.delete()
        summary = ", ".join(f"{k}xx: {v}" for k, v in sorted(counts.items()))
        r.add(f"Endpoint sweep ({len(paths)} GET endpoints as {user.email})", not failures, summary)
        for f in failures[:40]:
            self.stdout.write(self.style.ERROR(f"    {f}"))
        if denied and self.verbosity > 1:
            self.stdout.write(f"    Refused for {user.email} ({len(denied)}): " + ", ".join(denied[:60]))

    # ── entry point ───────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        self.verbosity = int(opts.get("verbosity", 1))
        r = Result()
        self.check_database(r)
        self.check_redis_celery(r)
        self.check_elasticsearch(r)
        self.check_milvus(r)
        self.check_models(r, opts["chat"])
        self.check_onlyoffice(r, opts.get("onlyoffice_file_id"))
        self.check_nextcloud(r)
        self.check_email(r, opts.get("email_to"))
        if not opts["skip_endpoints"]:
            self.sweep_endpoints(r, opts.get("as_email"))

        width = max(len(a) for a, _, _ in r.rows)
        self.stdout.write("")
        for area, ok, detail in r.rows:
            mark = self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL")
            self.stdout.write(f"  {mark}  {area.ljust(width)}  {detail}")
        failed = [a for a, ok, _ in r.rows if not ok]
        self.stdout.write("")
        if failed:
            self.stdout.write(self.style.ERROR(f"{len(failed)} check(s) failed: {', '.join(failed)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("All checks passed."))
