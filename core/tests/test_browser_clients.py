"""The browser signs in with PKCE and holds no client secret: no service may require one.

Requiring X-Client-Secret on service endpoints made every OCR, translation, anonymisation, redlining,
versioning, workflow, clustering and analytics call from the browser fail with 400 once the secret was
(rightly) removed from the browser. Identity comes from the bearer token. Only sign-in and token refresh,
used by server-to-server clients, take a client secret.
"""
import pathlib
import re

from django.test import TestCase

from core.tests.test_security_audit import Base, api

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALLOWED = {"custom_authentication/views.py"}          # login / refresh for confidential clients
REQUIRES_SECRET = re.compile(r"all\(\[[^\]]*\bclient_secret\b|\bnot client_secret\b|Missing X-Client-ID or X-Client-Secret")


class NoServiceRequiresClientSecretTests(TestCase):
    def test_no_view_requires_a_client_secret(self):
        offenders = []
        for path in ROOT.glob("*/**/*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED or "/tests" in rel or rel.split("/")[-1].startswith("test") or \
                    rel.startswith(("awaiting_deployment/", "scripts/")) or "/migrations/" in rel:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if REQUIRES_SECRET.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "Service endpoints must not require a client secret:\n" + "\n".join(offenders))


class BrowserStyleRequestTests(Base):
    def test_insights_analytics_answer_a_browser_request(self):
        """Bearer token + X-Client-ID + empty X-Client-Secret, exactly as the browser sends it."""
        c = api(self.tok_a1, HTTP_X_CLIENT_ID=self.tok_a1.application.client_id, HTTP_X_CLIENT_SECRET="")
        for path in ("/api/v1/service-analytics/overview/", "/api/v1/service-analytics/recent-activity/",
                     "/api/v1/service-analytics/tool-data/"):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, f"{path}: {r.status_code} {r.content[:200]}")
