"""Cloud storage: Google Drive, Dropbox, Microsoft OneDrive, Microsoft Teams (channel files) and Box."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

from .base import Api, ConnectorError, Provider, http_request, item

# ── Google Drive ─────────────────────────────────────────────────────────────

GOOGLE_FOLDER = "application/vnd.google-apps.folder"
GOOGLE_EXPORTS = {   # Google-native files are exported to an Office format on import
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}
GDRIVE = "https://www.googleapis.com/drive/v3"


class GoogleDrive(Provider):
    key, name, category = "google-drive", "Google Drive", "Cloud Storage"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = ["openid", "email", "https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]
    client_id_env, client_secret_env = "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"
    extra_auth_params = {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
    export_hint = "Uploads into the open Google Drive folder."

    def account(self, api):
        return api.json("GET", "https://www.googleapis.com/oauth2/v3/userinfo").get("email", "")

    def list(self, api, folder_id, page_token):
        params = {"q": f"'{(folder_id or 'root').replace(chr(39), '')}' in parents and trashed = false",
                  "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)", "pageSize": 200,
                  "orderBy": "folder,name_natural", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        data = api.json("GET", f"{GDRIVE}/files", params=params)
        out = []
        for f in data.get("files", []):
            folder = f.get("mimeType") == GOOGLE_FOLDER
            name = f.get("name", "")
            if f.get("mimeType") in GOOGLE_EXPORTS:
                name += GOOGLE_EXPORTS[f["mimeType"]][1]
            out.append(item(f["id"], name, "folder" if folder else "file", int(f["size"]) if f.get("size") else None,
                            f.get("modifiedTime"), f.get("mimeType")))
        return out, data.get("nextPageToken")

    def download(self, api, item_id):
        meta = api.json("GET", f"{GDRIVE}/files/{quote(item_id)}", params={"fields": "name,mimeType", "supportsAllDrives": "true"})
        mime = meta.get("mimeType", "")
        if mime == GOOGLE_FOLDER:
            raise ConnectorError("Folders cannot be imported; open the folder and choose its files.")
        if mime in GOOGLE_EXPORTS:
            target, ext = GOOGLE_EXPORTS[mime]
            return meta["name"] + ext, api.stream(f"{GDRIVE}/files/{quote(item_id)}/export", params={"mimeType": target})
        if mime.startswith("application/vnd.google-apps"):
            raise ConnectorError(f"{meta.get('name')} is a Google item that cannot be downloaded as a file.")
        return meta["name"], api.stream(f"{GDRIVE}/files/{quote(item_id)}", params={"alt": "media", "supportsAllDrives": "true"})

    def upload(self, api, folder_id, filename, path, size):
        start = api.post("https://www.googleapis.com/upload/drive/v3/files",
                         params={"uploadType": "resumable", "supportsAllDrives": "true"},
                         json={"name": filename, "parents": [folder_id or "root"]},
                         headers={"X-Upload-Content-Length": str(size)})
        location = start.headers.get("Location")
        if not location:
            raise ConnectorError("Google Drive did not start the upload.")
        with open(path, "rb") as fh:
            data = api.request("PUT", location, data=fh, headers={"Content-Length": str(size)}).json()
        return {"id": data.get("id"), "name": data.get("name", filename)}

    def revoke(self, api):
        http_request("POST", "https://oauth2.googleapis.com/revoke", params={"token": api.account.access_token})


# ── Dropbox ──────────────────────────────────────────────────────────────────

class Dropbox(Provider):
    key, name, category = "dropbox", "Dropbox", "Cloud Storage"
    authorize_url = "https://www.dropbox.com/oauth2/authorize"
    token_url = "https://api.dropboxapi.com/oauth2/token"
    scopes = ["account_info.read", "files.metadata.read", "files.content.read", "files.content.write"]
    client_id_env, client_secret_env = "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"
    extra_auth_params = {"token_access_type": "offline"}
    export_hint = "Uploads into the open Dropbox folder."
    max_upload_bytes = 150 * 1024 * 1024       # single-request upload limit

    def account(self, api):
        return api.json("POST", "https://api.dropboxapi.com/2/users/get_current_account").get("email", "")

    def list(self, api, folder_id, page_token):
        if page_token:
            data = api.json("POST", "https://api.dropboxapi.com/2/files/list_folder/continue", json={"cursor": page_token})
        else:
            data = api.json("POST", "https://api.dropboxapi.com/2/files/list_folder",
                            json={"path": folder_id or "", "limit": 200, "include_non_downloadable_files": False})
        out = [item(e["id"], e["name"], "folder" if e[".tag"] == "folder" else "file", e.get("size"),
                    e.get("server_modified"), path=e.get("path_display"))
               for e in data.get("entries", []) if e.get(".tag") in ("file", "folder")]
        out.sort(key=lambda i: (i["type"] != "folder", i["name"].lower()))
        return out, data.get("cursor") if data.get("has_more") else None

    def download(self, api, item_id):
        resp = api.request("POST", "https://content.dropboxapi.com/2/files/download", stream=True,
                           headers={"Dropbox-API-Arg": json.dumps({"path": item_id})})
        meta = json.loads(resp.headers.get("Dropbox-API-Result") or "{}")
        from .base import CHUNK, capped
        return meta.get("name") or "dropbox-file", capped(resp.iter_content(CHUNK))

    def upload(self, api, folder_id, filename, path, size):
        folder = ""
        if folder_id:
            folder = api.json("POST", "https://api.dropboxapi.com/2/files/get_metadata", json={"path": folder_id}).get("path_display", "")
        with open(path, "rb") as fh:
            data = api.json("POST", "https://content.dropboxapi.com/2/files/upload", data=fh, headers={
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": json.dumps({"path": f"{folder}/{filename}", "mode": "add", "autorename": True})})
        return {"id": data.get("id"), "name": data.get("name", filename)}

    def revoke(self, api):
        api.request("POST", "https://api.dropboxapi.com/2/auth/token/revoke", retry=False)


# ── Microsoft Graph (OneDrive, Teams) ────────────────────────────────────────

GRAPH = "https://graph.microsoft.com/v1.0"
SMALL_UPLOAD = 4 * 1024 * 1024
GRAPH_CHUNK = 320 * 1024 * 16            # 5 MiB, a multiple of 320 KiB as Graph requires


def _graph_items(data: dict) -> list[dict]:
    return [item(i["id"], i["name"], "folder" if "folder" in i else "file", i.get("size"), i.get("lastModifiedDateTime"),
                 (i.get("file") or {}).get("mimeType")) for i in data.get("value", [])]


def _graph_next(data: dict) -> str | None:
    nxt = data.get("@odata.nextLink")
    return nxt if nxt and nxt.startswith(GRAPH + "/") else None


def _graph_upload(api: Api, parent_url: str, filename: str, path: str, size: int) -> dict:
    """parent_url: .../items/{parent-id}  ->  small PUT, or an upload session in 5 MiB chunks."""
    name = quote(filename)
    if size <= SMALL_UPLOAD:
        with open(path, "rb") as fh:
            data = api.json("PUT", f"{parent_url}:/{name}:/content", data=fh.read(),
                            headers={"Content-Type": "application/octet-stream"})
        return {"id": data.get("id"), "name": data.get("name", filename)}
    session = api.json("POST", f"{parent_url}:/{name}:/createUploadSession",
                       json={"item": {"@microsoft.graph.conflictBehavior": "rename"}})
    url, data, offset = session["uploadUrl"], {}, 0
    with open(path, "rb") as fh:
        while offset < size:
            chunk = fh.read(GRAPH_CHUNK)
            end = offset + len(chunk) - 1
            resp = http_request("PUT", url, data=chunk, headers={"Content-Length": str(len(chunk)),
                                                                 "Content-Range": f"bytes {offset}-{end}/{size}"})
            if resp.status_code >= 400:
                raise ConnectorError(f"The upload was interrupted ({resp.status_code}).")
            offset = end + 1
            if resp.status_code in (200, 201):
                data = resp.json()
    return {"id": data.get("id"), "name": data.get("name", filename)}


class _Microsoft(Provider):
    category = "Cloud Storage"
    token_url = ""
    client_id_env, client_secret_env = "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"

    @property
    def tenant(self):
        return os.getenv("MICROSOFT_TENANT", "common").strip() or "common"

    @property
    def authorize_url(self):
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    def token_endpoint(self, extra=None):
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    def refresh(self, refresh_token, extra):
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                    "scope": " ".join(self.scopes)}, extra)

    def account(self, api):
        me = api.json("GET", f"{GRAPH}/me", params={"$select": "mail,userPrincipalName"})
        return me.get("mail") or me.get("userPrincipalName", "")


class OneDrive(_Microsoft):
    key, name = "onedrive", "Microsoft OneDrive"
    scopes = ["offline_access", "User.Read", "Files.ReadWrite"]
    export_hint = "Uploads into the open OneDrive folder."

    def list(self, api, folder_id, page_token):
        url = page_token or (f"{GRAPH}/me/drive/items/{quote(folder_id)}/children" if folder_id else f"{GRAPH}/me/drive/root/children")
        params = None if page_token else {"$top": 200, "$select": "id,name,size,file,folder,lastModifiedDateTime"}
        data = api.json("GET", url, params=params)
        return _graph_items(data), _graph_next(data)

    def download(self, api, item_id):
        meta = api.json("GET", f"{GRAPH}/me/drive/items/{quote(item_id)}", params={"$select": "name,folder"})
        if "folder" in meta:
            raise ConnectorError("Folders cannot be imported; open the folder and choose its files.")
        return meta["name"], api.stream(f"{GRAPH}/me/drive/items/{quote(item_id)}/content")

    def upload(self, api, folder_id, filename, path, size):
        return _graph_upload(api, f"{GRAPH}/me/drive/items/{quote(folder_id or 'root')}", filename, path, size)


class Teams(_Microsoft):
    """Files shared in Microsoft Teams: teams -> channels -> the channel's files (a SharePoint folder)."""
    key, name, category = "microsoft-teams", "Microsoft Teams", "Collaboration"
    scopes = ["offline_access", "User.Read", "Team.ReadBasic.All", "Channel.ReadBasic.All", "Files.ReadWrite.All"]
    export_hint = "Open a channel (or a folder in it), then export: files go to that channel's Files tab."

    def _channel_folder(self, api, team, channel):
        f = api.json("GET", f"{GRAPH}/teams/{quote(team)}/channels/{quote(channel)}/filesFolder")
        return f["parentReference"]["driveId"], f["id"]

    def list(self, api, folder_id, page_token):
        if page_token:
            data = api.json("GET", page_token)
            drive = (folder_id or "").split(":")[1] if (folder_id or "").startswith("item:") else ""
            return self._drive_items(data, drive), _graph_next(data)
        if not folder_id:
            data = api.json("GET", f"{GRAPH}/me/joinedTeams", params={"$select": "id,displayName"})
            return [item(f"team:{t['id']}", t["displayName"], "folder") for t in data.get("value", [])], None
        kind, *rest = folder_id.split(":")
        if kind == "team":
            data = api.json("GET", f"{GRAPH}/teams/{quote(rest[0])}/channels", params={"$select": "id,displayName"})
            return [item(f"channel:{rest[0]}:{c['id']}", c["displayName"], "folder") for c in data.get("value", [])], None
        if kind == "channel":
            drive, folder = self._channel_folder(api, rest[0], rest[1])
        elif kind == "item":
            drive, folder = rest[0], rest[1]
        else:
            raise ConnectorError("Unknown Teams location.")
        data = api.json("GET", f"{GRAPH}/drives/{quote(drive)}/items/{quote(folder)}/children",
                        params={"$top": 200, "$select": "id,name,size,file,folder,lastModifiedDateTime"})
        return self._drive_items(data, drive), _graph_next(data)

    def _drive_items(self, data, drive):
        return [{**i, "id": f"item:{drive}:{i['id']}"} for i in _graph_items(data)]

    def download(self, api, item_id):
        kind, drive, iid = (item_id.split(":") + ["", ""])[:3]
        if kind != "item":
            raise ConnectorError("Choose a file inside a channel.")
        meta = api.json("GET", f"{GRAPH}/drives/{quote(drive)}/items/{quote(iid)}", params={"$select": "name,folder"})
        if "folder" in meta:
            raise ConnectorError("Folders cannot be imported; open the folder and choose its files.")
        return meta["name"], api.stream(f"{GRAPH}/drives/{quote(drive)}/items/{quote(iid)}/content")

    def upload(self, api, folder_id, filename, path, size):
        kind, *rest = (folder_id or "").split(":")
        if kind == "channel":
            drive, folder = self._channel_folder(api, rest[0], rest[1])
        elif kind == "item":
            drive, folder = rest[0], rest[1]
        else:
            raise ConnectorError("Open a channel (or a folder inside one) to export to Teams.")
        return _graph_upload(api, f"{GRAPH}/drives/{quote(drive)}/items/{quote(folder)}", filename, path, size)


# ── Box ──────────────────────────────────────────────────────────────────────

BOX = "https://api.box.com/2.0"


class Box(Provider):
    key, name, category = "box", "Box", "Cloud Storage"
    authorize_url = "https://account.box.com/api/oauth2/authorize"
    token_url = "https://api.box.com/oauth2/token"
    scopes = []                         # set on the Box app (Read and write all files and folders)
    client_id_env, client_secret_env = "BOX_CLIENT_ID", "BOX_CLIENT_SECRET"
    pkce = False
    export_hint = "Uploads into the open Box folder."
    max_upload_bytes = 50 * 1024 * 1024        # single-request upload limit

    def account(self, api):
        return api.json("GET", f"{BOX}/users/me", params={"fields": "login"}).get("login", "")

    def list(self, api, folder_id, page_token):
        offset = int(page_token or 0)
        data = api.json("GET", f"{BOX}/folders/{quote(folder_id or '0')}/items",
                        params={"fields": "id,type,name,size,modified_at", "limit": 200, "offset": offset})
        out = [item(e["id"], e["name"], "folder" if e["type"] == "folder" else "file", e.get("size"), e.get("modified_at"))
               for e in data.get("entries", []) if e.get("type") in ("file", "folder")]
        nxt = offset + len(data.get("entries", []))
        return out, str(nxt) if nxt < int(data.get("total_count", 0)) else None

    def download(self, api, item_id):
        meta = api.json("GET", f"{BOX}/files/{quote(item_id)}", params={"fields": "name"})
        return meta["name"], api.stream(f"{BOX}/files/{quote(item_id)}/content")

    def upload(self, api, folder_id, filename, path, size):
        from .dev import _with_suffix
        for _ in range(2):
            with open(path, "rb") as fh:
                resp = api.request("POST", "https://upload.box.com/api/2.0/files/content", allow=(409,), files={
                    "attributes": (None, json.dumps({"name": filename, "parent": {"id": folder_id or "0"}}), "application/json"),
                    "file": (filename, fh)})
            if resp.status_code != 409:
                entry = (resp.json().get("entries") or [{}])[0]
                return {"id": entry.get("id"), "name": entry.get("name", filename)}
            filename = _with_suffix(filename)          # 409: a file with that name is already there
        raise ConnectorError("Box refused the file name.")

    def revoke(self, api):
        http_request("POST", "https://api.box.com/oauth2/revoke",
                     data={"client_id": self.client_id(), "client_secret": self.client_secret(), "token": api.account.access_token})
