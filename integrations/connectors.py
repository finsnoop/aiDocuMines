"""
Connector service layer: OAuth sign-in, per-user API access, background import/export jobs and webhooks.

Views stay thin; everything that touches tokens or files lives here.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import tempfile
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache

from .models import ConnectorAccount, ConnectorJob
from .providers import Api, ConnectorError, get_provider
from .providers.base import pkce_pair, save_tokens
from .providers.webhooks import ALLOWED_HOOK_HOSTS, assert_public_https, event_body, sign

logger = logging.getLogger(__name__)

STATE_TTL = 600                    # seconds a sign-in may take
MAX_JOB_FILES = 100


# ── OAuth ────────────────────────────────────────────────────────────────────

def public_api_base(request) -> str:
    """Where the provider sends the browser back. INTEGRATIONS_PUBLIC_API_URL wins (set it behind proxies)."""
    base = os.getenv("INTEGRATIONS_PUBLIC_API_URL", "").strip().rstrip("/")
    return base or request.build_absolute_uri("/").rstrip("/")


def redirect_uri_for(request, service: str) -> str:
    return f"{public_api_base(request)}/api/v1/integrations/{service}/auth/callback/"


def start_oauth(request, provider) -> str:
    if not provider.configured():
        raise ConnectorError(f"{provider.name} is not set up yet. An administrator needs to add its app keys.")
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair() if provider.pkce else (None, None)
    redirect_uri = redirect_uri_for(request, provider.key)
    cache.set(f"oauth-state:{state}", {"user": request.user.id, "service": provider.key, "verifier": verifier,
                                       "redirect_uri": redirect_uri}, STATE_TTL)
    return provider.auth_url(state, redirect_uri, challenge)


def finish_oauth(state: str, code: str) -> ConnectorAccount:
    """Exchange the code; the state is single-use and names the user who started the sign-in."""
    from django.contrib.auth import get_user_model
    saved = cache.get(f"oauth-state:{state}") if state else None
    if not saved:
        raise ConnectorError("This sign-in link has expired or was already used. Start again from Integrations.")
    cache.delete(f"oauth-state:{state}")
    provider = get_provider(saved["service"])
    user = get_user_model().objects.get(pk=saved["user"])
    tokens = provider.exchange(code, saved["redirect_uri"], saved["verifier"])
    acc, _ = ConnectorAccount.objects.get_or_create(user=user, service=provider.key)
    acc.refresh_token = ""
    acc.expires_at = None
    save_tokens(acc, tokens)
    api = Api(provider, acc)
    try:
        acc.extra = {**(acc.extra or {}), **provider.after_connect(api, tokens)}
        acc.account_label = (tokens.get("label") or provider.account(api) or "")[:255]
    except ConnectorError as exc:
        logger.info("%s connected but the account details failed: %s", provider.key, exc)
    acc.save()
    return acc


def frontend_redirect(**params) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/app/integrations?{urlencode(params)}"


# ── access ───────────────────────────────────────────────────────────────────

def account_for(user, service: str) -> ConnectorAccount | None:
    return ConnectorAccount.objects.filter(user=user, service=service).first()


def api_for(user, provider) -> Api:
    acc = account_for(user, provider.key)
    if not acc or not acc.access_token:
        raise ConnectorError(f"Connect {provider.name} first.")
    return Api(provider, acc)


def status_for(user, provider) -> dict:
    acc = account_for(user, provider.key)
    connected = bool(acc and (acc.access_token or acc.extra.get("webhook_url")))
    out = {**provider.describe(), "is_authenticated": connected, "connected": connected,
           "user_email": acc.account_label if acc else None,
           "expires_at": acc.expires_at.isoformat() if acc and acc.expires_at else None}
    if connected and provider.kind == "webhook":
        out["webhook_url"] = _mask(acc.extra.get("webhook_url", ""))
    return out


def _mask(url: str) -> str:
    return url[:32] + "…" if len(url) > 36 else url


def safe_part(value: str, fallback: str) -> str:
    """A matter / service name used as a folder name: no separators or traversal."""
    from core.access import safe_filename
    try:
        return safe_filename(value) if value else fallback
    except ValueError:
        return fallback


# ── jobs ─────────────────────────────────────────────────────────────────────

def create_job(user, provider, kind: str, payload: dict) -> ConnectorJob:
    ids = [str(i) for i in (payload.get("file_ids") or []) if str(i).strip()]
    if not ids:
        raise ConnectorError("Choose at least one file.")
    if len(ids) > MAX_JOB_FILES:
        raise ConnectorError(f"Choose at most {MAX_JOB_FILES} files at a time.")
    api_for(user, provider)                    # fail now, not in the background, when not connected
    request = {**payload, "file_ids": ids}
    if kind == "import":
        # Every document enters through a matter, exactly like Matters -> Upload (core/ingest.py).
        request = {"file_ids": ids, "matter_id": str(_matter(user, payload.get("matter_id")).id)}
    job = ConnectorJob.objects.create(user=user, service=provider.key, kind=kind, request=request)
    from .tasks import run_connector_job
    run_connector_job.delay(job.id)
    return job


def job_payload(job: ConnectorJob) -> dict:
    ok = sum(1 for r in job.results if r.get("status") == "success")
    bad = sum(1 for r in job.results if r.get("status") == "failed")
    return {"job_id": job.id, "run_id": str(job.id), "kind": job.kind, "service": job.service, "status": job.status,
            "files": job.results, "total_files": len(job.request.get("file_ids", [])), "successful": ok, "failed": bad,
            "error": job.error, "created_at": job.created_at}


def run_job(job: ConnectorJob):
    provider = get_provider(job.service)
    job.status = "running"
    job.save(update_fields=["status", "updated_at"])
    try:
        api = api_for(job.user, provider)
        if job.kind == "import":
            _import(job, provider, api)
        else:
            _export(job, provider, api)
        job.status = "done"
    except ConnectorError as exc:
        job.status, job.error = "failed", str(exc)
    except Exception:
        logger.exception("Connector job %s failed", job.id)
        job.status, job.error = "failed", "The transfer stopped unexpectedly. Try again."
    job.save()
    notify(job.user, f"{job.kind}.completed", {"service": job.service, **{k: v for k, v in job_payload(job).items()
                                                                          if k in ("job_id", "status", "successful", "failed")}})


def _matter(user, matter_id):
    from core.ingest import matter_for_ingest
    try:
        return matter_for_ingest(user, matter_id)
    except ValueError as exc:
        raise ConnectorError(str(exc))


def entry_result(entry: dict, **extra) -> dict:
    """A MatterBatch outcome as a job result: uploaded / added / duplicate count as success."""
    ok = entry.get("status") in ("uploaded", "added", "duplicate")
    out = {**extra, "file_id": entry.get("file_id"), "filename": entry.get("filename"),
           "status": "success" if ok else "failed", "outcome": entry.get("status"), "message": entry.get("message")}
    if not ok:
        out["error"] = entry.get("message") or "This file could not be added."
    return out


def _import(job, provider, api):
    from core.ingest import MatterBatch
    user, req = job.user, job.request
    batch = MatterBatch(user, _matter(user, req["matter_id"]), source=provider.key)
    results = []
    for ext_id in req["file_ids"]:
        try:
            name, chunks = provider.download(api, ext_id)
            results.append(entry_result(batch.add_stream(name, chunks), external_id=ext_id))
        except ConnectorError as exc:
            results.append({"external_id": ext_id, "file_id": None, "filename": None, "status": "failed", "error": str(exc)})
        except Exception:
            logger.exception("Import of %s from %s failed", ext_id, provider.key)
            results.append({"external_id": ext_id, "file_id": None, "filename": None, "status": "failed",
                            "error": "This file could not be imported."})
        job.results = results
        job.save(update_fields=["results", "updated_at"])
    batch.run.status = "Completed" if any(r["status"] == "success" for r in results) else "Failed"
    batch.run.save(update_fields=["status"])
    batch.finish()


def _export(job, provider, api):
    from core.access import get_file_for_user
    results = []
    folder = job.request.get("target_folder_id") or None
    for fid in job.request["file_ids"]:
        try:
            f = get_file_for_user(job.user, fid, "download")
            path = f.filepath if f.filepath and os.path.isfile(f.filepath) else (f.filepath or "").replace("/app/", "", 1)
            if not os.path.isfile(path):
                raise ConnectorError("This document's contents are missing from storage.")
            size = os.path.getsize(path)
            if provider.max_upload_bytes and size > provider.max_upload_bytes:
                raise ConnectorError(f"{f.filename} is larger than {provider.name} accepts in one upload "
                                     f"({provider.max_upload_bytes // (1024 * 1024)} MB).")
            if provider.key == "notion":
                from core.assistant import file_text
                out = provider.upload(api, folder, f.filename, path, size, text=file_text(f))
            else:
                out = provider.upload(api, folder, f.filename, path, size)
            results.append({"file_id": str(f.id), "filename": f.filename, "external_id": out.get("id"), "status": "success"})
        except ConnectorError as exc:
            results.append({"file_id": str(fid), "filename": None, "status": "failed", "error": str(exc)})
        except Exception as exc:
            if exc.__class__.__name__ == "Http404":
                results.append({"file_id": str(fid), "filename": None, "status": "failed", "error": "Document not found."})
            else:
                logger.exception("Export of %s to %s failed", fid, provider.key)
                results.append({"file_id": str(fid), "filename": None, "status": "failed", "error": "This document could not be exported."})
        job.results = results
        job.save(update_fields=["results", "updated_at"])


# ── webhooks (Zapier, Make) ──────────────────────────────────────────────────

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect_webhook(request, provider, url: str, matter_id=None) -> dict:
    url = assert_public_https(url, ALLOWED_HOOK_HOSTS.get(provider.key))
    matter = _matter(request.user, matter_id)       # where inbound documents go unless a request names another
    acc, _ = ConnectorAccount.objects.get_or_create(user=request.user, service=provider.key)
    secret, inbound = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    acc.webhook_secret = secret
    acc.inbound_token_hash = _hash(inbound)
    acc.extra = {**(acc.extra or {}), "webhook_url": url, "default_matter_id": str(matter.id), "default_matter": matter.name}
    acc.account_label = request.user.email or ""
    acc.save()
    return {"is_authenticated": True, "connected": True, "signing_secret": secret, "default_matter": matter.name,
            "inbound_url": f"{public_api_base(request)}/api/v1/integrations/hooks/{inbound}/upload/",
            "message": "Keep the signing secret and upload URL safe: they are shown only once."}


def account_for_inbound(token: str) -> ConnectorAccount | None:
    if not token or len(token) < 20:
        return None
    return ConnectorAccount.objects.filter(inbound_token_hash=_hash(token)).select_related("user").first()


def notify(user, event: str, data: dict):
    """Queue an event for every webhook connection the user has (failures never affect the caller)."""
    try:
        ids = list(ConnectorAccount.objects.filter(user=user, service__in=("zapier", "make"))
                   .exclude(extra__webhook_url=None).values_list("id", flat=True))
        if ids:
            from .tasks import deliver_webhook
            for acc_id in ids:
                deliver_webhook.delay(acc_id, event, data)
    except Exception:
        logger.exception("Could not queue %s webhooks", event)


def deliver(acc: ConnectorAccount, event: str, data: dict) -> int:
    from .providers.base import http_request
    url = assert_public_https(acc.extra.get("webhook_url", ""), ALLOWED_HOOK_HOSTS.get(acc.service))
    body = event_body(event, data)
    resp = http_request("POST", url, data=body, allow_redirects=False, timeout=(5, 15), headers={
        "Content-Type": "application/json", "User-Agent": "aiDocuMines-Webhooks/1.0",
        "X-aiDocuMines-Event": event, "X-aiDocuMines-Signature": sign(acc.webhook_secret, body)})
    return resp.status_code


def fetch_public_file(url: str, max_bytes: int) -> tuple[str, str]:
    """Download a file named by an inbound webhook into a temp file (public https only, size-capped)."""
    from .providers.base import http_request
    url = assert_public_https(url)
    resp = http_request("GET", url, stream=True, allow_redirects=False, timeout=(5, 60))
    if resp.status_code != 200:
        raise ConnectorError(f"The file address returned {resp.status_code}.")
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "file"
    fd, tmp = tempfile.mkstemp()
    total = 0
    with os.fdopen(fd, "wb") as fh:
        for c in resp.iter_content(1024 * 1024):
            total += len(c)
            if total > max_bytes:
                fh.close()
                os.remove(tmp)
                raise ConnectorError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
            fh.write(c)
    return tmp, name
