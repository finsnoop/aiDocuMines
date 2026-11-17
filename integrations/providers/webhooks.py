"""
Automation: Zapier and Make, connected by webhooks (no OAuth app needed).

Outbound  aiDocuMines -> your Zap / Scenario: events (document.uploaded, import.completed, export.completed, test)
          POSTed as JSON to the webhook URL you paste, signed with HMAC-SHA256 so the receiver can verify them:
              X-aiDocuMines-Signature: sha256=<hex of HMAC(secret, body)>
Inbound   your Zap / Scenario -> aiDocuMines: POST a file (multipart "file") or {"file_url": ...} to the private
          upload URL shown once at connection time. Files land in the connected user's documents.

Only public https addresses are accepted, so neither direction can be pointed at the platform's own network.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
from urllib.parse import urlparse

from .base import ConnectorError, Provider

ALLOWED_HOOK_HOSTS = {
    "zapier": ("hooks.zapier.com",),
    "make": ("hook.eu1.make.com", "hook.eu2.make.com", "hook.us1.make.com", "hook.us2.make.com", "hook.make.com",
             "hook.integromat.com"),
}


def assert_public_https(url: str, allowed_hosts: tuple[str, ...] | None = None) -> str:
    """Reject anything but https to a public address (no localhost, private, link-local or metadata ranges)."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConnectorError("Use an https:// address.")
    if parsed.username or parsed.password:
        raise ConnectorError("Addresses with embedded credentials are not accepted.")
    host = parsed.hostname.lower()
    if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise ConnectorError(f"Use the webhook address your automation tool gives you ({', '.join(allowed_hosts[:2])}…).")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ConnectorError("That address does not resolve.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ConnectorError("That address points to a private or internal network.")
    return parsed.geturl()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def event_body(event: str, data: dict) -> bytes:
    from django.utils import timezone
    return json.dumps({"event": event, "sent_at": timezone.now().isoformat(), "data": data},
                      default=str, separators=(",", ":")).encode()


class _Webhook(Provider):
    kind = "webhook"
    category = "Automation"
    can_import = False          # documents arrive through the inbound URL instead
    can_export = False
    pkce = False

    def configured(self) -> bool:
        return True             # nothing for an administrator to set up

    def setup_env(self):
        return []

    def describe(self):
        return {**super().describe(), "events": ["document.uploaded", "import.completed", "export.completed"]}


class Zapier(_Webhook):
    key, name = "zapier", "Zapier"


class Make(_Webhook):
    key, name = "make", "Make (Integromat)"
