"""
Connectors on the Integrations page: OAuth sign-in, every provider's browse / import / export against its
documented API responses (the network is replaced by a fake), background jobs, webhooks and isolation.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from integrations import connectors as svc
from integrations.models import ConnectorAccount, ConnectorJob
from integrations.providers import PROVIDERS, Api, ConnectorError, get_provider

TMP_MEDIA = tempfile.mkdtemp(prefix="connectors-media-")
KEYS = {k: "test-value" for k in (
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET", "BOX_CLIENT_ID", "BOX_CLIENT_SECRET", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET",
    "NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITLAB_CLIENT_ID",
    "GITLAB_CLIENT_SECRET", "ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET", "SALESFORCE_CLIENT_ID",
    "SALESFORCE_CLIENT_SECRET", "HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET")}
KEYS["INTEGRATIONS_PUBLIC_API_URL"] = "https://api.example.test"


# ── fake network ─────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, json_body=None, content=b"", headers=None):
        self.status_code, self._json, self.headers = status, json_body, headers or {}
        self.content = content if content or json_body is None else json.dumps(json_body).encode()

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self.content), max(1, n)):
            yield self.content[i:i + n]


class Net:
    def __init__(self):
        self.routes, self.calls = [], []

    def on(self, method, pattern, *responses):
        self.routes.append([method, re.compile(pattern), list(responses)])
        return self

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, pat, resps in self.routes:
            if m == method and pat.search(url):
                r = resps[0] if len(resps) == 1 else resps.pop(0)
                return r(url, kw) if callable(r) else r
        raise AssertionError(f"unexpected {method} {url}")

    def find(self, method, pattern):
        return [c for c in self.calls if c[0] == method and re.search(pattern, c[1])]


@contextmanager
def network(net):
    targets = ["integrations.providers.base.http_request", "integrations.providers.storage.http_request",
               "integrations.providers.collab.http_request", "integrations.providers.crm.http_request"]
    patches = [mock.patch(t, net) for t in targets]
    for p in patches:
        p.start()
    try:
        yield net
    finally:
        for p in patches:
            p.stop()


def J(body, status=200, **headers):
    return FakeResp(status, body, headers=headers)


def B(content: bytes, status=200, **headers):
    return FakeResp(status, None, content, headers)


@override_settings(MEDIA_ROOT=TMP_MEDIA, FRONTEND_URL="https://app.example.test")
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other_firm = Client.objects.create(name="Firm B")
        cls.alice = make_user("alice@firm-a.test", cls.firm, groups=["Client"])
        cls.eve = make_user("eve@firm-b.test", cls.other_firm, groups=["Client"])
        cls.matter = Matter.objects.create(client=cls.firm, name="Atlas", created_by=cls.alice)
        cls.matter.members.add(cls.alice)
        cls.eves_matter = Matter.objects.create(client=cls.other_firm, name="Secret", created_by=cls.eve)
        cls.eves_matter.members.add(cls.eve)

    def setUp(self):
        cache.clear()
        env = mock.patch.dict(os.environ, KEYS)          # provider app keys for every test
        env.start()
        self.addCleanup(env.stop)
        self.dir = tempfile.mkdtemp(prefix="conn-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def account(self, service, user=None, **extra):
        return ConnectorAccount.objects.create(user=user or self.alice, service=service, access_token="tok",
                                               refresh_token="ref", extra=extra,
                                               expires_at=timezone.now() + timedelta(hours=1))

    def api(self, service, **extra):
        return Api(get_provider(service), self.account(service, **extra))

    def local_file(self, name="contract.pdf", data=b"%PDF-1.4 hello", user=None):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        owner = user or self.alice
        run = Run.objects.create(user=owner, status="Completed")
        return File.objects.create(filename=name, filepath=path, user=owner, run=run, project_id="Atlas",
                                   service_id="matter", md5_hash=os.urandom(8).hex(), file_size=len(data),
                                   content="Clause 1. The parties agree.")

    def tmpfile(self, data=b"0123456789"):
        path = os.path.join(self.dir, "up.bin")
        with open(path, "wb") as fh:
            fh.write(data)
        return path, len(data)


# ── catalogue and OAuth ──────────────────────────────────────────────────────

class CatalogAndOAuthTests(Base):
    def test_catalog_lists_every_connector_with_its_state(self):
        r = self.client_for(self.alice).get("/api/v1/integrations/catalog/").json()
        services = {c["service"]: c for c in r["connectors"]}
        self.assertEqual(set(services), set(PROVIDERS))
        self.assertTrue(services["google-drive"]["configured"])
        self.assertFalse(services["google-drive"]["connected"])
        self.assertNotIn("setup", services["google-drive"])          # setup details are for administrators

    def test_unconfigured_connector_says_so(self):
        with mock.patch.dict(os.environ, {"DROPBOX_APP_KEY": ""}):
            r = self.client_for(self.alice).post("/api/v1/integrations/dropbox/auth/initiate/")
        self.assertEqual(r.status_code, 409)
        self.assertIn("not set up", r.json()["error"])

    def test_sign_in_round_trip_stores_encrypted_tokens_for_the_right_user(self):
        r = self.client_for(self.alice).post("/api/v1/integrations/google-drive/auth/initiate/").json()
        q = parse_qs(urlparse(r["auth_url"]).query)
        self.assertEqual(q["redirect_uri"], ["https://api.example.test/api/v1/integrations/google-drive/auth/callback/"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        state = q["state"][0]
        net = Net().on("POST", r"oauth2\.googleapis\.com/token", J({"access_token": "AT-1", "refresh_token": "RT-1",
                                                                     "expires_in": 3600, "scope": "drive"}))
        net.on("GET", r"userinfo", J({"email": "alice@gmail.test"}))
        with network(net):
            resp = APIClient().get(f"/api/v1/integrations/google-drive/auth/callback/?state={state}&code=CODE")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://app.example.test/app/integrations?connected=google-drive")
        sent = parse_qs(net.find("POST", "token")[0][2]["data"] and "&".join(
            f"{k}={v}" for k, v in net.find("POST", "token")[0][2]["data"].items()))
        self.assertIn("code_verifier", sent)                         # PKCE
        acc = ConnectorAccount.objects.get(user=self.alice, service="google-drive")
        self.assertEqual((acc.access_token, acc.refresh_token, acc.account_label), ("AT-1", "RT-1", "alice@gmail.test"))
        with connection.cursor() as cur:
            cur.execute("SELECT access_token FROM integrations_connectoraccount WHERE id = %s", [acc.id])
            self.assertNotIn("AT-1", cur.fetchone()[0])              # encrypted at rest
        # the state is single-use
        resp = APIClient().get(f"/api/v1/integrations/google-drive/auth/callback/?state={state}&code=CODE")
        self.assertIn("error=", resp["Location"])

    def test_provider_error_comes_back_to_the_page(self):
        resp = APIClient().get("/api/v1/integrations/dropbox/auth/callback/?error=access_denied")
        self.assertIn("error=access_denied", resp["Location"])

    def test_expired_token_is_refreshed_before_the_call(self):
        acc = self.account("dropbox")
        acc.expires_at = timezone.now() - timedelta(minutes=5)
        acc.save()
        net = Net().on("POST", r"api\.dropboxapi\.com/oauth2/token", J({"access_token": "fresh", "expires_in": 14400}))
        net.on("POST", r"files/list_folder$", J({"entries": [], "has_more": False}))
        with network(net):
            get_provider("dropbox").list(Api(get_provider("dropbox"), acc), None, None)
        self.assertEqual(net.find("POST", r"list_folder")[0][2]["headers"]["Authorization"], "Bearer fresh")

    def test_revoke_disconnects(self):
        self.account("box")
        net = Net().on("POST", r"oauth2/revoke", J({}))
        with network(net):
            r = self.client_for(self.alice).post("/api/v1/integrations/box/auth/revoke/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ConnectorAccount.objects.filter(user=self.alice, service="box").exists())

    def test_files_need_a_connection_and_unknown_services_404(self):
        c = self.client_for(self.alice)
        self.assertIn("Connect", c.get("/api/v1/integrations/github/files/").json()["error"])
        self.assertEqual(c.get("/api/v1/integrations/myspace/files/").status_code, 404)


# ── providers ────────────────────────────────────────────────────────────────

class StorageProviderTests(Base):
    def test_google_drive(self):
        p, api = get_provider("google-drive"), self.api("google-drive")
        net = Net().on("GET", r"drive/v3/files$", J({"nextPageToken": "n2", "files": [
            {"id": "f1", "name": "Deals", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "d1", "name": "Memo", "mimeType": "application/vnd.google-apps.document"},
            {"id": "p1", "name": "nda.pdf", "mimeType": "application/pdf", "size": "1200"}]}))
        net.on("GET", r"files/d1$", J({"name": "Memo", "mimeType": "application/vnd.google-apps.document"}))
        net.on("GET", r"files/d1/export", B(b"DOCX"))
        net.on("POST", r"upload/drive/v3/files", J({}, Location="https://www.googleapis.com/upload/session/abc"))
        net.on("PUT", r"upload/session/abc", J({"id": "new1", "name": "x.pdf"}))
        with network(net):
            items, nxt = p.list(api, "f0", None)
            self.assertEqual([(i["name"], i["type"]) for i in items],
                             [("Deals", "folder"), ("Memo.docx", "file"), ("nda.pdf", "file")])
            self.assertEqual(nxt, "n2")
            self.assertIn("'f0' in parents", net.find("GET", r"drive/v3/files$")[0][2]["params"]["q"])
            name, chunks = p.download(api, "d1")
            self.assertEqual((name, b"".join(chunks)), ("Memo.docx", b"DOCX"))
            path, size = self.tmpfile()
            self.assertEqual(p.upload(api, "f0", "x.pdf", path, size)["id"], "new1")
        self.assertEqual(net.find("POST", "upload")[0][2]["json"]["parents"], ["f0"])

    def test_dropbox(self):
        p, api = get_provider("dropbox"), self.api("dropbox")
        net = Net().on("POST", r"files/list_folder$", J({"has_more": True, "cursor": "c1", "entries": [
            {".tag": "file", "id": "id:2", "name": "b.pdf", "size": 5}, {".tag": "folder", "id": "id:1", "name": "A"}]}))
        net.on("POST", r"files/download", B(b"PDF", **{"Dropbox-API-Result": json.dumps({"name": "b.pdf"})}))
        net.on("POST", r"get_metadata", J({"path_display": "/A"}))
        net.on("POST", r"files/upload", J({"id": "id:9", "name": "x.pdf"}))
        with network(net):
            items, nxt = p.list(api, None, None)
            self.assertEqual([i["name"] for i in items], ["A", "b.pdf"])        # folders first
            self.assertEqual(nxt, "c1")
            self.assertEqual(p.download(api, "id:2")[0], "b.pdf")
            path, size = self.tmpfile()
            p.upload(api, "id:1", "x.pdf", path, size)
        arg = json.loads(net.find("POST", "files/upload")[0][2]["headers"]["Dropbox-API-Arg"])
        self.assertEqual((arg["path"], arg["autorename"]), ("/A/x.pdf", True))

    def test_onedrive_small_and_large_uploads(self):
        p, api = get_provider("onedrive"), self.api("onedrive")
        net = Net().on("GET", r"me/drive/root/children", J({"value": [{"id": "1", "name": "Docs", "folder": {}}],
                                                             "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}))
        net.on("PUT", r"items/root:/small\.pdf:/content", J({"id": "s1", "name": "small.pdf"}))
        net.on("POST", r"createUploadSession", J({"uploadUrl": "https://upload.example.test/sess"}))
        net.on("PUT", r"upload\.example\.test/sess", J({}, 202), J({"id": "L1", "name": "big.bin"}, 201))
        with network(net):
            items, nxt = p.list(api, None, None)
            self.assertEqual((items[0]["type"], nxt), ("folder", "https://graph.microsoft.com/v1.0/next"))
            path, size = self.tmpfile(b"x" * 100)
            self.assertEqual(p.upload(api, None, "small.pdf", path, size)["id"], "s1")
            big, bsize = self.tmpfile(b"y" * (6 * 1024 * 1024))
            self.assertEqual(p.upload(api, None, "big.bin", big, bsize)["id"], "L1")
        ranges = [c[2]["headers"]["Content-Range"] for c in net.find("PUT", "sess")]
        self.assertEqual(ranges[0], f"bytes 0-{5 * 1024 * 1024 - 1}/{6 * 1024 * 1024}")

    def test_teams_teams_channels_files(self):
        p, api = get_provider("microsoft-teams"), self.api("microsoft-teams")
        net = Net().on("GET", r"me/joinedTeams", J({"value": [{"id": "T1", "displayName": "Legal"}]}))
        net.on("GET", r"teams/T1/channels$", J({"value": [{"id": "C1", "displayName": "General"}]}))
        net.on("GET", r"filesFolder", J({"id": "F1", "parentReference": {"driveId": "D1"}}))
        net.on("GET", r"drives/D1/items/F1/children", J({"value": [{"id": "I1", "name": "brief.docx", "file": {}}]}))
        net.on("GET", r"drives/D1/items/I1$", J({"name": "brief.docx"}))
        net.on("GET", r"drives/D1/items/I1/content", B(b"DOCX"))
        with network(net):
            self.assertEqual(p.list(api, None, None)[0][0]["id"], "team:T1")
            self.assertEqual(p.list(api, "team:T1", None)[0][0]["id"], "channel:T1:C1")
            files, _ = p.list(api, "channel:T1:C1", None)
            self.assertEqual(files[0]["id"], "item:D1:I1")
            self.assertEqual(p.download(api, "item:D1:I1")[0], "brief.docx")
            with self.assertRaises(ConnectorError):
                p.upload(api, None, "x", *self.tmpfile())          # must choose a channel

    def test_box_paging_and_name_conflict(self):
        p, api = get_provider("box"), self.api("box")
        net = Net().on("GET", r"folders/0/items", J({"total_count": 3, "entries": [
            {"type": "folder", "id": "9", "name": "Deals"}, {"type": "file", "id": "5", "name": "a.pdf", "size": 3}]}))
        net.on("POST", r"upload\.box\.com", J({"code": "item_name_in_use"}, 409), J({"entries": [{"id": "77", "name": "x (1).pdf"}]}))
        with network(net):
            items, nxt = p.list(api, None, None)
            self.assertEqual((len(items), nxt), (2, "2"))
            self.assertEqual(p.upload(api, None, "x.pdf", *self.tmpfile())["id"], "77")


class CollaborationProviderTests(Base):
    def test_slack(self):
        p, api = get_provider("slack"), self.api("slack")
        net = Net().on("GET", r"conversations\.list", J({"ok": True, "channels": [{"id": "C1", "name": "deals", "is_member": True}]}))
        net.on("GET", r"files\.list", J({"ok": True, "files": [{"id": "F1", "name": "nda.pdf", "size": 9,
                                                                "url_private_download": "https://files.slack.com/x"}],
                                          "paging": {"page": 1, "pages": 2}}))
        net.on("GET", r"files\.info", J({"ok": True, "file": {"name": "nda.pdf", "url_private_download": "https://files.slack.com/x"}}))
        net.on("GET", r"files\.slack\.com/x", B(b"PDF"))
        net.on("GET", r"getUploadURLExternal", J({"ok": True, "upload_url": "https://files.slack.com/upload/1", "file_id": "F9"}))
        net.on("POST", r"files\.slack\.com/upload/1", B(b"OK"))
        net.on("POST", r"completeUploadExternal", J({"ok": True, "files": [{"id": "F9", "title": "x.pdf"}]}))
        with network(net):
            roots, _ = p.list(api, None, None)
            self.assertEqual([r["id"] for r in roots], ["mine", "channel:C1"])
            files, nxt = p.list(api, "channel:C1", None)
            self.assertEqual((files[0]["name"], nxt), ("nda.pdf", "2"))
            self.assertEqual(b"".join(p.download(api, "F1")[1]), b"PDF")
            self.assertEqual(p.upload(api, "channel:C1", "x.pdf", *self.tmpfile())["id"], "F9")
        self.assertEqual(net.find("POST", "completeUploadExternal")[0][2]["json"]["channel_id"], "C1")

    def test_slack_reports_revoked_tokens_clearly(self):
        net = Net().on("GET", r"conversations\.list", J({"ok": False, "error": "token_revoked"}))
        with network(net), self.assertRaisesMessage(ConnectorError, "Connect it again"):
            get_provider("slack").list(self.api("slack"), None, None)

    def test_notion_pages_as_markdown_and_export(self):
        p, api = get_provider("notion"), self.api("notion")
        page = {"id": "P1", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Deal memo"}]}}}
        net = Net().on("POST", r"v1/search", J({"results": [page], "has_more": False}))
        net.on("GET", r"v1/pages/P1", J(page))
        net.on("GET", r"blocks/P1/children", J({"has_more": False, "results": [
            {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Terms"}]}},
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "Price 1m"}]}},
            {"id": "P2", "type": "child_page", "child_page": {"title": "Annex"}}]}))
        net.on("POST", r"v1/pages$", J({"id": "NEW"}))
        with network(net):
            roots, _ = p.list(api, None, None)
            self.assertEqual((roots[0]["name"], roots[0]["type"]), ("Deal memo", "folder"))
            inside, _ = p.list(api, "P1", None)
            self.assertEqual([i["name"] for i in inside], ["Deal memo.md", "Annex"])
            name, chunks = p.download(api, "P1")
            text = b"".join(chunks).decode()
            self.assertEqual(name, "Deal memo.md")
            self.assertIn("## Terms", text)
            self.assertIn("- Price 1m", text)
            out = p.upload(api, "P1", "contract.pdf", *self.tmpfile(), text="A" * 2500 + "\n\nB")
        self.assertEqual(out["id"], "NEW")
        sent = net.find("POST", r"v1/pages$")[0][2]["json"]
        self.assertEqual(sent["parent"], {"page_id": "P1"})
        self.assertEqual(len(sent["children"]), 3)                   # 2000-char limit split + second paragraph


class DevProviderTests(Base):
    def test_github(self):
        p, api = get_provider("github"), self.api("github")
        net = Net().on("GET", r"user/repos", J([{"full_name": "acme/deals"}]))
        net.on("GET", r"repos/acme/deals/contents/$", J([{"type": "dir", "name": "docs", "path": "docs"},
                                                          {"type": "file", "name": "a.pdf", "path": "a.pdf", "size": 4}]))
        net.on("GET", r"contents/a\.pdf$", B(b"RAW"))
        net.on("GET", r"contents/docs/x\.pdf$", J({"sha": "abc"}))                     # already exists
        net.on("PUT", r"contents/docs/x%20", J({"content": {"name": "x (stamp).pdf"}}))
        with network(net):
            self.assertEqual(p.list(api, None, None)[0][0]["id"], "repo:acme/deals")
            items, _ = p.list(api, "repo:acme/deals", None)
            self.assertEqual([i["id"] for i in items], ["path:acme/deals:docs", "path:acme/deals:a.pdf"])
            self.assertEqual(b"".join(p.download(api, "path:acme/deals:a.pdf")[1]), b"RAW")
            p.upload(api, "path:acme/deals:docs", "x.pdf", *self.tmpfile())
        put = net.find("PUT", "contents")[0]
        self.assertEqual(base64.b64decode(put[2]["json"]["content"]), b"0123456789")

    def test_gitlab(self):
        p, api = get_provider("gitlab"), self.api("gitlab")
        net = Net().on("GET", r"api/v4/projects$", J([{"id": 7, "path_with_namespace": "acme/deals"}], **{"X-Next-Page": "2"}))
        net.on("GET", r"api/v4/projects/7$", J({"default_branch": "main"}))
        net.on("GET", r"repository/tree", J([{"type": "blob", "name": "a.pdf", "path": "a.pdf"}]))
        net.on("POST", r"repository/files/", J({"message": "exists"}, 400), J({"file_path": "x"}, 201))
        with network(net):
            items, nxt = p.list(api, None, None)
            self.assertEqual((items[0]["id"], nxt), ("project:7", "2"))
            self.assertEqual(p.list(api, "project:7", None)[0][0]["id"], "path:7:a.pdf")
            out = p.upload(api, "project:7", "x.pdf", *self.tmpfile())
        self.assertTrue(out["id"].startswith("path:7:x ("))          # renamed after the name conflict

    def test_jira(self):
        p, api = get_provider("jira"), self.api("jira", sites=[{"id": "CL", "name": "acme"}])
        jira = r"ex/jira/CL/rest/api/3"
        net = Net().on("GET", jira + r"/project/search", J({"isLast": True, "values": [{"key": "LEG", "name": "Legal"}]}))
        net.on("GET", jira + r"/search/jql", J({"issues": [{"key": "LEG-1", "fields": {"summary": "NDA"}}]}))
        net.on("GET", jira + r"/issue/LEG-1$", J({"fields": {"attachment": [{"id": "55", "filename": "nda.pdf", "size": 3}]}}))
        net.on("GET", jira + r"/attachment/55$", J({"filename": "nda.pdf"}))
        net.on("GET", jira + r"/attachment/content/55", B(b"PDF"))
        net.on("POST", jira + r"/issue/LEG-1/attachments", J([{"id": "66", "filename": "x.pdf"}]))
        with network(net):
            self.assertEqual(p.list(api, None, None)[0][0]["id"], "site:CL")
            self.assertEqual(p.list(api, "site:CL", None)[0][0]["id"], "project:CL:LEG")
            self.assertEqual(p.list(api, "project:CL:LEG", None)[0][0]["id"], "issue:CL:LEG-1")
            self.assertEqual(p.list(api, "issue:CL:LEG-1", None)[0][0]["id"], "att:CL:55")
            self.assertEqual(p.download(api, "att:CL:55")[0], "nda.pdf")
            self.assertEqual(p.upload(api, "issue:CL:LEG-1", "x.pdf", *self.tmpfile())["id"], "att:CL:66")
        self.assertEqual(net.find("POST", "attachments")[0][2]["headers"]["X-Atlassian-Token"], "no-check")


class CrmProviderTests(Base):
    def test_salesforce(self):
        p, api = get_provider("salesforce"), self.api("salesforce", instance_url="https://acme.my.salesforce.com")
        net = Net().on("GET", r"query", lambda url, kw: J({"records": [
            {"Id": "069A", "Title": "NDA", "FileExtension": "pdf", "ContentSize": 3, "LatestPublishedVersionId": "068V"}],
            "nextRecordsUrl": "/services/data/v60.0/query/01g-2000"}))
        net.on("GET", r"ContentVersion/068V/VersionData", B(b"PDF"))
        net.on("POST", r"sobjects/ContentVersion", J({"id": "068N", "success": True}))
        with network(net):
            items, nxt = p.list(api, None, None)
            self.assertEqual((items[0]["name"], nxt), ("NDA.pdf", "/services/data/v60.0/query/01g-2000"))
            with self.assertRaises(ConnectorError):
                p.list(api, None, "https://evil.test/steal")           # page tokens must be Salesforce query paths
            self.assertEqual(p.download(api, "069A")[0], "NDA.pdf")
            with self.assertRaises(ConnectorError):
                p.download(api, "069A' OR Id != '")                   # no SOQL injection through ids
            self.assertEqual(p.upload(api, None, "x.pdf", *self.tmpfile())["id"], "068N")

    def test_hubspot(self):
        p, api = get_provider("hubspot"), self.api("hubspot")
        net = Net().on("GET", r"folders/search", J({"results": [{"id": "10", "name": "Deals"},
                                                                {"id": "11", "name": "Nested", "parentFolderId": "10"}]}))
        net.on("GET", r"files/search", J({"results": [{"id": "20", "name": "nda", "extension": "pdf"},
                                                      {"id": "21", "name": "inner", "parentFolderId": "10"}]}))
        net.on("GET", r"files/v3/files/20$", J({"name": "nda", "extension": "pdf"}))
        net.on("GET", r"files/20/signed-url", J({"url": "https://cdn.hubspot.test/signed"}))
        net.on("GET", r"cdn\.hubspot\.test/signed", B(b"PDF"))
        net.on("POST", r"files/v3/files$", J({"id": "30", "name": "x.pdf"}))
        with network(net):
            items, _ = p.list(api, None, None)
            self.assertEqual([i["name"] for i in items], ["Deals", "nda.pdf"])    # top level only
            name, chunks = p.download(api, "20")
            self.assertEqual((name, b"".join(chunks)), ("nda.pdf", b"PDF"))
            self.assertEqual(p.upload(api, "10", "x.pdf", *self.tmpfile())["id"], "30")


# ── jobs ─────────────────────────────────────────────────────────────────────

def run_now(job_id):
    from integrations.tasks import run_connector_job
    run_connector_job(job_id)


@mock.patch("integrations.tasks.run_connector_job.delay", side_effect=run_now)
@mock.patch("integrations.tasks.deliver_webhook.delay")
class JobTests(Base):
    def test_import_job_goes_through_the_matter_pipeline(self, _hook, _delay):
        self.account("dropbox")
        net = Net().on("POST", r"files/download", lambda url, kw: (
            B(b"%PDF-1.4 x", **{"Dropbox-API-Result": json.dumps({"name": "../nda.pdf"})})
            if json.loads(kw["headers"]["Dropbox-API-Arg"])["path"] in ("id:1", "id:1-again") else J({"error": "x"}, 404)))
        with network(net):
            r = self.client_for(self.alice).post("/api/v1/integrations/dropbox/import/",
                                                 {"file_ids": ["id:1", "id:gone", "id:1-again"], "matter_id": str(self.matter.id)},
                                                 format="json")
        self.assertEqual(r.status_code, 202, r.content)
        job = self.client_for(self.alice).get(f"/api/v1/integrations/jobs/{r.json()['job_id']}/").json()
        self.assertEqual((job["status"], job["successful"], job["failed"]), ("done", 2, 1))
        f = File.objects.get(pk=job["files"][0]["file_id"])
        # stored and recorded exactly as Matters -> Upload does it
        self.assertEqual((f.user, f.filename, f.project_id, f.service_id), (self.alice, "nda.pdf", "Atlas", "matter"))
        self.assertIn(os.path.join("Atlas", "matter"), f.filepath)
        self.assertTrue(f.filepath.startswith(TMP_MEDIA))
        from document_operations.models import FileFolderLink
        self.assertTrue(FileFolderLink.objects.filter(file=f).exists())          # in the sidebar's tree
        self.assertIn("no longer exists", job["files"][1]["error"])
        self.assertEqual(job["files"][2]["outcome"], "duplicate")               # same content: not stored twice
        self.assertEqual(File.objects.filter(user=self.alice, filename="nda.pdf").count(), 1)
        # someone else cannot read the job
        self.assertEqual(self.client_for(self.eve).get(f"/api/v1/integrations/jobs/{job['job_id']}/").status_code, 404)

    def test_import_needs_a_matter_the_user_is_on(self, _hook, _delay):
        self.account("dropbox")
        c = self.client_for(self.alice)
        self.assertIn("Choose the matter", c.post("/api/v1/integrations/dropbox/import/", {"file_ids": ["id:1"]}, format="json").json()["error"])
        r = c.post("/api/v1/integrations/dropbox/import/", {"file_ids": ["id:1"], "matter_id": str(self.eves_matter.id)}, format="json")
        self.assertIn("not found", r.json()["error"])
        self.matter.status = "closed"
        self.matter.save()
        r = c.post("/api/v1/integrations/dropbox/import/", {"file_ids": ["id:1"], "matter_id": str(self.matter.id)}, format="json")
        self.assertIn("closed", r.json()["error"])

    def test_export_job_uploads_only_the_users_own_documents(self, _hook, _delay):
        self.account("box")
        mine, theirs = self.local_file("mine.pdf"), self.local_file("secret.pdf", user=self.eve)
        net = Net().on("POST", r"upload\.box\.com", J({"entries": [{"id": "1", "name": "mine.pdf"}]}))
        with network(net):
            r = self.client_for(self.alice).post("/api/v1/integrations/box/export/",
                                                 {"file_ids": [mine.id, theirs.id], "target_folder_id": "0"}, format="json")
        job = ConnectorJob.objects.get(pk=r.json()["job_id"])
        self.assertEqual([x["status"] for x in job.results], ["success", "failed"])
        self.assertEqual(len(net.find("POST", "upload")), 1)          # the other firm's file never left
        self.assertEqual(job.results[1]["error"], "Document not found.")

    def test_export_to_notion_sends_the_documents_text(self, _hook, _delay):
        self.account("notion")
        doc = self.local_file("memo.pdf")
        net = Net().on("POST", r"v1/pages$", J({"id": "N1"}))
        with network(net):
            self.client_for(self.alice).post("/api/v1/integrations/notion/export/",
                                             {"file_ids": [doc.id], "target_folder_id": "P1"}, format="json")
        blocks = net.find("POST", "pages")[0][2]["json"]["children"]
        self.assertIn("The parties agree", blocks[0]["paragraph"]["rich_text"][0]["text"]["content"])

    def test_limits_and_unsupported_directions(self, _hook, _delay):
        self.account("github")
        c = self.client_for(self.alice)
        self.assertIn("at least one", c.post("/api/v1/integrations/github/import/", {"file_ids": []}, format="json").json()["error"])
        self.assertIn("at most", c.post("/api/v1/integrations/github/import/",
                                        {"file_ids": [str(i) for i in range(101)]}, format="json").json()["error"])
        self.assertEqual(c.post("/api/v1/integrations/zapier/import/", {"file_ids": ["1"]}, format="json").status_code, 405)


# ── webhooks (Zapier, Make) ──────────────────────────────────────────────────

PUBLIC = [(2, 1, 6, "", ("44.0.0.1", 443))]
PRIVATE = [(2, 1, 6, "", ("10.0.0.5", 443))]


class WebhookTests(Base):
    def connect(self, url="https://hooks.zapier.com/hooks/catch/1/abc/", dns=PUBLIC, service="zapier", matter=True):
        body = {"webhook_url": url, **({"matter_id": str(self.matter.id)} if matter else {})}
        with mock.patch("integrations.providers.webhooks.socket.getaddrinfo", return_value=dns):
            return self.client_for(self.alice).post(f"/api/v1/integrations/{service}/connect/", body, format="json")

    def test_only_public_https_hooks_of_the_right_service(self):
        self.assertIn("https", self.connect("http://hooks.zapier.com/x").json()["error"])
        self.assertIn("private", self.connect(dns=PRIVATE).json()["error"])
        self.assertIn("webhook address", self.connect("https://evil.test/hook").json()["error"])
        self.assertIn("credentials", self.connect("https://u:p@hooks.zapier.com/x").json()["error"])
        self.assertIn("Choose the matter", self.connect(matter=False).json()["error"])
        r = self.connect()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["inbound_url"].startswith("https://api.example.test/api/v1/integrations/hooks/"))

    def test_events_are_signed(self):
        secret = self.connect().json()["signing_secret"]
        acc = ConnectorAccount.objects.get(user=self.alice, service="zapier")
        net = Net().on("POST", r"hooks\.zapier\.com", B(b"ok"))
        with network(net), mock.patch("integrations.providers.webhooks.socket.getaddrinfo", return_value=PUBLIC):
            self.assertEqual(svc.deliver(acc, "document.uploaded", {"file_id": 1}), 200)
        call = net.calls[0][2]
        expected = "sha256=" + hmac.new(secret.encode(), call["data"], hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"]["X-aiDocuMines-Signature"], expected)
        self.assertFalse(call["allow_redirects"])                     # a redirect cannot bounce us inward

    def test_inbound_upload_saves_into_the_connected_users_documents(self):
        token = self.connect().json()["inbound_url"].rstrip("/").split("/")[-2]
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = APIClient().post(f"/api/v1/integrations/hooks/{token}/upload/",
                             {"file": SimpleUploadedFile("lead.pdf", b"%PDF-1.4 x")})
        self.assertEqual(r.status_code, 201, r.content)
        f = File.objects.get(pk=r.json()["file_id"])
        self.assertEqual((f.user, f.project_id, f.service_id, f.filename), (self.alice, "Atlas", "matter", "lead.pdf"))
        # a matter the connected user is not on is refused
        r = APIClient().post(f"/api/v1/integrations/hooks/{token}/upload/",
                             {"file": SimpleUploadedFile("x.pdf", b"%PDF-1.4 y"), "matter_id": str(self.eves_matter.id)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(APIClient().post("/api/v1/integrations/hooks/wrong-token-0123456789abc/upload/").status_code, 404)

    def test_inbound_file_urls_cannot_reach_internal_addresses(self):
        token = self.connect().json()["inbound_url"].rstrip("/").split("/")[-2]
        with mock.patch("integrations.providers.webhooks.socket.getaddrinfo", return_value=PRIVATE):
            r = APIClient().post(f"/api/v1/integrations/hooks/{token}/upload/",
                                 {"file_url": "https://metadata.internal/latest"}, format="json")
        self.assertIn("private", r.json()["error"])

    @mock.patch("integrations.tasks.deliver_webhook.delay")
    def test_new_documents_raise_an_event(self, delay):
        self.connect()
        with self.captureOnCommitCallbacks(execute=True):
            self.local_file("new.pdf")
        event = delay.call_args[0][1]
        self.assertEqual(event, "document.uploaded")
