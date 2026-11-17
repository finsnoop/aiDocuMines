"""
Connector framework: one class per external service, all behind the same five operations.

    auth_url / exchange / refresh    OAuth 2.0 authorisation-code flow (PKCE where the provider supports it)
    account(api)                      a label for the connected account (email or username)
    list(api, folder_id, page_token)  -> (items, next_page_token)   items as Item dicts (see `item`)
    download(api, item_id)            -> (filename, iterator of bytes)
    upload(api, folder_id, filename, path, size) -> {"id", "name"}

Every HTTP call goes through `http_request` so tests can replace the network in one place.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import timedelta
from typing import Iterator
from urllib.parse import urlencode

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT = (10, 120)              # connect, read (seconds)
CHUNK = 1024 * 1024
MAX_IMPORT_BYTES = int(os.getenv("CONNECTOR_MAX_IMPORT_MB", "500")) * 1024 * 1024


class ConnectorError(Exception):
    """A problem the user can act on (shown as is)."""


class NotSupported(ConnectorError):
    pass


def http_request(method: str, url: str, **kwargs) -> requests.Response:
    """The single network entry point (tests patch this)."""
    kwargs.setdefault("timeout", TIMEOUT)
    return requests.request(method, url, **kwargs)


def item(id: str, name: str, type: str = "file", size: int | None = None, modified: str | None = None,
         mime: str | None = None, path: str | None = None, can_download: bool | None = None) -> dict:
    """The shape the Integrations file browser expects (ConnectorFile in the frontend)."""
    return {"id": str(id), "name": name or "(untitled)", "type": type, "size": size, "modified_date": modified,
            "mime_type": mime, "path": path, "can_download": (type == "file") if can_download is None else can_download}


class Api:
    """Authenticated calls for one connected account: bearer token, refreshed once on expiry or a 401."""

    def __init__(self, provider: "Provider", account):
        self.provider, self.account = provider, account

    def _token(self) -> str:
        acc = self.account
        if acc.expires_at and acc.refresh_token and acc.expires_at <= timezone.now() + timedelta(seconds=60):
            self.refresh()
        return acc.access_token

    def refresh(self):
        acc = self.account
        if not acc.refresh_token:
            raise ConnectorError(f"Your {self.provider.name} sign-in has expired. Connect it again.")
        tokens = self.provider.refresh(acc.refresh_token, acc.extra or {})
        save_tokens(acc, tokens)

    def headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", **self.provider.default_headers}
        return {**h, **(extra or {})}

    def request(self, method: str, url: str, *, headers: dict | None = None, retry: bool = True,
                allow: tuple = (), **kw) -> requests.Response:
        """`allow`: statuses the caller handles itself (e.g. 404 "not there yet", 409 "name taken")."""
        resp = http_request(method, url, headers=self.headers(headers), **kw)
        if resp.status_code == 401 and retry and self.account.refresh_token:
            self.refresh()
            resp = http_request(method, url, headers=self.headers(headers), **kw)
        if resp.status_code in allow:
            return resp
        if resp.status_code == 401:
            raise ConnectorError(f"{self.provider.name} no longer accepts this connection. Connect it again.")
        if resp.status_code == 403:
            raise ConnectorError(f"{self.provider.name} refused access to that item.")
        if resp.status_code == 404:
            raise ConnectorError(f"That item no longer exists in {self.provider.name}.")
        if resp.status_code == 429:
            raise ConnectorError(f"{self.provider.name} is rate-limiting requests. Try again in a minute.")
        if resp.status_code >= 400:
            logger.info("%s %s -> %s %s", method, url, resp.status_code, (resp.text or "")[:300])
            raise ConnectorError(f"{self.provider.name} returned an error ({resp.status_code}).")
        return resp

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def json(self, method, url, **kw):
        return self.request(method, url, **kw).json()

    def stream(self, url, **kw) -> Iterator[bytes]:
        resp = self.request("GET", url, stream=True, **kw)
        return capped(resp.iter_content(CHUNK))


def capped(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Stop a download that grows past the import limit."""
    total = 0
    for c in chunks:
        total += len(c)
        if total > MAX_IMPORT_BYTES:
            raise ConnectorError(f"The file is larger than the {MAX_IMPORT_BYTES // (1024 * 1024)} MB import limit.")
        yield c


def save_tokens(acc, tokens: dict):
    acc.access_token = tokens.get("access_token") or acc.access_token
    if tokens.get("refresh_token"):
        acc.refresh_token = tokens["refresh_token"]
    if tokens.get("expires_in"):
        acc.expires_at = timezone.now() + timedelta(seconds=int(tokens["expires_in"]))
    if tokens.get("scope"):
        acc.scopes = str(tokens["scope"])[:2000]
    acc.save()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class Provider:
    key = ""
    name = ""
    category = ""
    kind = "oauth"                 # oauth | webhook
    authorize_url = ""
    token_url = ""
    scopes: list[str] = []
    scope_param = "scope"
    scope_sep = " "
    client_id_env = ""
    client_secret_env = ""
    pkce = True
    token_auth = "body"            # body | basic | json
    extra_auth_params: dict = {}
    default_headers: dict = {}
    can_import = True
    can_export = True
    export_hint = ""               # where exports land, for the UI
    max_upload_bytes = 0           # 0 = no extra limit

    # ── configuration ──
    def client_id(self) -> str:
        return os.getenv(self.client_id_env, "").strip()

    def client_secret(self) -> str:
        return os.getenv(self.client_secret_env, "").strip()

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def setup_env(self) -> list[str]:
        return [self.client_id_env, self.client_secret_env]

    # ── OAuth ──
    def auth_url(self, state: str, redirect_uri: str, challenge: str | None) -> str:
        params = {"client_id": self.client_id(), "redirect_uri": redirect_uri, "response_type": "code", "state": state}
        if self.scopes:
            params[self.scope_param] = self.scope_sep.join(self.scopes)
        if self.pkce and challenge:
            params.update(code_challenge=challenge, code_challenge_method="S256")
        params.update(self.extra_auth_params)
        return f"{self.authorize_url}?{urlencode(params)}"

    def _token_request(self, data: dict, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        auth = None
        if self.token_auth == "basic":
            auth = (self.client_id(), self.client_secret())
        else:
            data = {**data, "client_id": self.client_id(), "client_secret": self.client_secret()}
        if self.token_auth == "json" or (self.token_auth == "basic" and getattr(self, "token_json", False)):
            resp = http_request("POST", self.token_endpoint(extra), json=data, headers=headers, auth=auth)
        else:
            resp = http_request("POST", self.token_endpoint(extra), data=data, headers=headers, auth=auth)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or body.get("error") or (body.get("ok") is False):
            logger.info("%s token exchange failed: %s %s", self.key, resp.status_code, str(body)[:300])
            raise ConnectorError(f"{self.name} did not accept the sign-in. Try connecting again.")
        return self.normalise_tokens(body)

    def token_endpoint(self, extra: dict | None = None) -> str:
        return self.token_url

    def normalise_tokens(self, body: dict) -> dict:
        return body

    def exchange(self, code: str, redirect_uri: str, verifier: str | None) -> dict:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if self.pkce and verifier:
            data["code_verifier"] = verifier
        return self._token_request(data)

    def refresh(self, refresh_token: str, extra: dict) -> dict:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, extra)

    def after_connect(self, api: Api, tokens: dict) -> dict:
        """Extra non-secret details to keep (instance URL, sites). Called once after sign-in."""
        return {}

    # ── operations (implemented per provider) ──
    def account(self, api: Api) -> str:
        return ""

    def list(self, api: Api, folder_id: str | None, page_token: str | None) -> tuple[list[dict], str | None]:
        raise NotSupported(f"Browsing {self.name} is not available.")

    def download(self, api: Api, item_id: str) -> tuple[str, Iterator[bytes]]:
        raise NotSupported(f"Importing from {self.name} is not available.")

    def upload(self, api: Api, folder_id: str | None, filename: str, path: str, size: int) -> dict:
        raise NotSupported(f"Exporting to {self.name} is not available.")

    def revoke(self, api: Api):
        """Best effort: tell the provider to forget the token."""

    def describe(self) -> dict:
        return {"service": self.key, "name": self.name, "category": self.category, "kind": self.kind,
                "configured": self.configured(), "can_import": self.can_import, "can_export": self.can_export,
                "export_hint": self.export_hint}
