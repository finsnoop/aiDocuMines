"""CRM & business: Salesforce Files and the HubSpot file manager."""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import quote

from .base import ConnectorError, Provider, http_request, item

# ── Salesforce ───────────────────────────────────────────────────────────────

SF_VERSION = "v60.0"


def _soql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Salesforce(Provider):
    """Salesforce Files (ContentDocument): browse newest first, import, and upload new files."""
    key, name, category = "salesforce", "Salesforce", "CRM & Business"
    scopes = ["api", "refresh_token"]
    client_id_env, client_secret_env = "SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET"
    export_hint = "Exports become Salesforce Files you own."
    max_upload_bytes = 35 * 1024 * 1024        # base64 JSON insert limit

    @property
    def login(self):
        return os.getenv("SALESFORCE_LOGIN_URL", "https://login.salesforce.com").rstrip("/")

    @property
    def authorize_url(self):
        return f"{self.login}/services/oauth2/authorize"

    def token_endpoint(self, extra=None):
        return f"{self.login}/services/oauth2/token"

    def after_connect(self, api, tokens):
        return {"instance_url": tokens.get("instance_url", "")}

    def _base(self, api) -> str:
        inst = (api.account.extra or {}).get("instance_url", "")
        if not inst.startswith("https://"):
            raise ConnectorError("The Salesforce connection is incomplete. Connect it again.")
        return f"{inst}/services/data/{SF_VERSION}"

    def account(self, api):
        inst = (api.account.extra or {}).get("instance_url", "")
        return api.json("GET", f"{inst}/services/oauth2/userinfo").get("email", "")

    def list(self, api, folder_id, page_token):
        base = self._base(api)
        if page_token:
            if not page_token.startswith(f"/services/data/{SF_VERSION}/query/"):
                raise ConnectorError("Unknown Salesforce page.")
            data = api.json("GET", (api.account.extra or {})["instance_url"] + page_token)
        else:
            q = ("SELECT Id, Title, FileExtension, ContentSize, LastModifiedDate FROM ContentDocument "
                 "ORDER BY LastModifiedDate DESC")
            data = api.json("GET", f"{base}/query", params={"q": q})
        out = [item(r["Id"], f"{r['Title']}.{r['FileExtension']}" if r.get("FileExtension") else r["Title"], "file",
                    r.get("ContentSize"), r.get("LastModifiedDate")) for r in data.get("records", [])]
        return out, data.get("nextRecordsUrl")

    def download(self, api, item_id):
        base = self._base(api)
        if not item_id.isalnum():
            raise ConnectorError("Unknown Salesforce file.")
        q = f"SELECT LatestPublishedVersionId, Title, FileExtension FROM ContentDocument WHERE Id = '{_soql_quote(item_id)}'"
        rows = api.json("GET", f"{base}/query", params={"q": q}).get("records", [])
        if not rows:
            raise ConnectorError("That Salesforce file no longer exists.")
        r = rows[0]
        name = f"{r['Title']}.{r['FileExtension']}" if r.get("FileExtension") else r["Title"]
        return name, api.stream(f"{base}/sobjects/ContentVersion/{r['LatestPublishedVersionId']}/VersionData")

    def upload(self, api, folder_id, filename, path, size):
        base = self._base(api)
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode()
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        made = api.json("POST", f"{base}/sobjects/ContentVersion",
                        json={"Title": title, "PathOnClient": filename, "VersionData": data})
        return {"id": made.get("id"), "name": filename}

    def revoke(self, api):
        http_request("POST", f"{self.login}/services/oauth2/revoke", data={"token": api.account.refresh_token or api.account.access_token})


# ── HubSpot ──────────────────────────────────────────────────────────────────

HS = "https://api.hubapi.com"


class HubSpot(Provider):
    """The HubSpot file manager: folders and files, import and upload."""
    key, name, category = "hubspot", "HubSpot", "CRM & Business"
    authorize_url = "https://app.hubspot.com/oauth/authorize"
    token_url = f"{HS}/oauth/v1/token"
    scopes = ["oauth", "files"]
    client_id_env, client_secret_env = "HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET"
    pkce = False
    export_hint = "Uploads into the open HubSpot folder as private files."

    def account(self, api):
        info = api.json("GET", f"{HS}/oauth/v1/access-tokens/{quote(api.account.access_token)}")
        return f"{info.get('user', '')} @ {info.get('hub_domain', '')}".strip(" @")

    def list(self, api, folder_id, page_token):
        # page_token "f:<after>" continues folders, "x:<after>" continues files
        out, nxt = [], None
        phase, _, after = (page_token or "f:").partition(":")
        if phase == "f":
            params = {"limit": 100, **({"after": after} if after else {})}
            if folder_id:
                params["parentFolderIds"] = folder_id
            data = api.json("GET", f"{HS}/files/v3/folders/search", params=params)
            out += [item(f["id"], f.get("name") or f.get("path"), "folder", modified=f.get("updatedAt"))
                    for f in data.get("results", []) if folder_id or not f.get("parentFolderId")]   # top level: no parent
            more = ((data.get("paging") or {}).get("next") or {}).get("after")
            if more:
                return out, f"f:{more}"
            after = ""
        params = {"limit": 100, **({"parentFolderIds": folder_id} if folder_id else {}), **({"after": after} if after else {})}
        data = api.json("GET", f"{HS}/files/v3/files/search", params=params)
        for f in data.get("results", []):
            if not folder_id and f.get("parentFolderId"):
                continue                                 # top level shows only files outside folders
            name = f.get("name", "file")
            if f.get("extension") and not name.lower().endswith("." + f["extension"].lower()):
                name = f"{name}.{f['extension']}"
            out.append(item(f["id"], name, "file", f.get("size"), f.get("updatedAt")))
        more = ((data.get("paging") or {}).get("next") or {}).get("after")
        return out, f"x:{more}" if more else nxt

    def download(self, api, item_id):
        meta = api.json("GET", f"{HS}/files/v3/files/{quote(item_id)}")
        signed = api.json("GET", f"{HS}/files/v3/files/{quote(item_id)}/signed-url")
        url = signed.get("url")
        if not url:
            raise ConnectorError("HubSpot did not provide a download link for that file.")
        name = meta.get("name", "hubspot-file")
        if meta.get("extension") and not name.lower().endswith("." + meta["extension"].lower()):
            name = f"{name}.{meta['extension']}"
        resp = http_request("GET", url, stream=True)        # the signed URL needs no token
        if resp.status_code >= 400:
            raise ConnectorError(f"HubSpot returned an error ({resp.status_code}).")
        from .base import CHUNK, capped
        return name, capped(resp.iter_content(CHUNK))

    def upload(self, api, folder_id, filename, path, size):
        fields = {"options": (None, json.dumps({"access": "PRIVATE", "overwrite": False}), "application/json"),
                  "fileName": (None, filename)}
        if folder_id:
            fields["folderId"] = (None, folder_id)
        else:
            fields["folderPath"] = (None, "/aiDocuMines")
        with open(path, "rb") as fh:
            fields["file"] = (filename, fh)
            data = api.json("POST", f"{HS}/files/v3/files", files=fields)
        return {"id": data.get("id"), "name": data.get("name", filename)}
