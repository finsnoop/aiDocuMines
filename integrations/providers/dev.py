"""Development: GitHub and GitLab (repository files) and Jira (issue attachments)."""
from __future__ import annotations

import base64
import os
import time
from urllib.parse import quote

from .base import ConnectorError, Provider, item

MAX_REPO_UPLOAD = 50 * 1024 * 1024


def _with_suffix(filename: str) -> str:
    stem, dot, ext = filename.rpartition(".")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stem} ({stamp}).{ext}" if dot else f"{filename} ({stamp})"


# ── GitHub ───────────────────────────────────────────────────────────────────

GH = "https://api.github.com"


class GitHub(Provider):
    """Repositories the person can access: browse, import files, and commit exported files."""
    key, name, category = "github", "GitHub", "Development"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    scopes = ["repo", "read:user", "user:email"]
    client_id_env, client_secret_env = "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"
    pkce = True
    default_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    export_hint = "Open a repository folder, then export: each document is committed there."
    max_upload_bytes = MAX_REPO_UPLOAD

    def account(self, api):
        return api.json("GET", f"{GH}/user").get("login", "")

    def list(self, api, folder_id, page_token):
        if not folder_id:
            page = int(page_token or 1)
            repos = api.json("GET", f"{GH}/user/repos", params={"per_page": 100, "page": page, "sort": "updated"})
            return ([item(f"repo:{r['full_name']}", r["full_name"], "folder", modified=r.get("updated_at")) for r in repos],
                    str(page + 1) if len(repos) == 100 else None)
        repo, path = self._split(folder_id)
        entries = api.json("GET", f"{GH}/repos/{repo}/contents/{quote(path)}")
        if isinstance(entries, dict):
            entries = [entries]
        out = [item(f"path:{repo}:{e['path']}", e["name"], "folder" if e["type"] == "dir" else "file", e.get("size"))
               for e in entries if e.get("type") in ("dir", "file")]
        out.sort(key=lambda i: (i["type"] != "folder", i["name"].lower()))
        return out, None

    @staticmethod
    def _split(folder_id: str) -> tuple[str, str]:
        kind, _, rest = folder_id.partition(":")
        if kind == "repo":
            return rest, ""
        if kind == "path":
            repo, _, path = rest.partition(":")        # "owner/repo:path/in/repo"
            return repo, path
        raise ConnectorError("Unknown GitHub location.")

    def download(self, api, item_id):
        repo, path = self._split(item_id)
        if not path:
            raise ConnectorError("Choose a file inside the repository.")
        return path.rsplit("/", 1)[-1], api.stream(f"{GH}/repos/{repo}/contents/{quote(path)}",
                                                    headers={"Accept": "application/vnd.github.raw+json"})

    def upload(self, api, folder_id, filename, path, size):
        if not folder_id:
            raise ConnectorError("Open a repository (or a folder in it) to export to GitHub.")
        repo, folder = self._split(folder_id)
        target = f"{folder}/{filename}".lstrip("/")
        if api.request("GET", f"{GH}/repos/{repo}/contents/{quote(target)}", allow=(404,)).status_code != 404:
            filename = _with_suffix(filename)
            target = f"{folder}/{filename}".lstrip("/")
        with open(path, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        data = api.json("PUT", f"{GH}/repos/{repo}/contents/{quote(target)}",
                        json={"message": f"Add {filename} from aiDocuMines", "content": content})
        return {"id": f"path:{repo}:{target}", "name": (data.get("content") or {}).get("name", filename)}


# ── GitLab ───────────────────────────────────────────────────────────────────

class GitLab(Provider):
    key, name, category = "gitlab", "GitLab", "Development"
    scopes = ["read_user", "api"]
    client_id_env, client_secret_env = "GITLAB_CLIENT_ID", "GITLAB_CLIENT_SECRET"
    export_hint = "Open a project folder, then export: each document is committed to the default branch."
    max_upload_bytes = MAX_REPO_UPLOAD

    @property
    def base(self):
        return os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")

    @property
    def authorize_url(self):
        return f"{self.base}/oauth/authorize"

    def token_endpoint(self, extra=None):
        return f"{self.base}/oauth/token"

    def account(self, api):
        return api.json("GET", f"{self.base}/api/v4/user").get("username", "")

    def _branch(self, api, pid: str) -> str:
        return api.json("GET", f"{self.base}/api/v4/projects/{quote(pid, safe='')}").get("default_branch") or "main"

    @staticmethod
    def _split(folder_id: str) -> tuple[str, str]:
        kind, _, rest = folder_id.partition(":")
        if kind == "project":
            return rest, ""
        if kind == "path":
            pid, _, path = rest.partition(":")
            return pid, path
        raise ConnectorError("Unknown GitLab location.")

    def list(self, api, folder_id, page_token):
        page = int(page_token or 1)
        if not folder_id:
            resp = api.get(f"{self.base}/api/v4/projects", params={"membership": "true", "per_page": 100, "page": page,
                                                                    "order_by": "last_activity_at", "simple": "true"})
            out = [item(f"project:{p['id']}", p["path_with_namespace"], "folder", modified=p.get("last_activity_at"))
                   for p in resp.json()]
            return out, resp.headers.get("X-Next-Page") or None
        pid, path = self._split(folder_id)
        resp = api.get(f"{self.base}/api/v4/projects/{quote(pid, safe='')}/repository/tree",
                       params={"path": path, "per_page": 100, "page": page, "ref": self._branch(api, pid)})
        out = [item(f"path:{pid}:{e['path']}", e["name"], "folder" if e["type"] == "tree" else "file")
               for e in resp.json() if e.get("type") in ("tree", "blob")]
        out.sort(key=lambda i: (i["type"] != "folder", i["name"].lower()))
        return out, resp.headers.get("X-Next-Page") or None

    def download(self, api, item_id):
        pid, path = self._split(item_id)
        if not path:
            raise ConnectorError("Choose a file inside the project.")
        url = f"{self.base}/api/v4/projects/{quote(pid, safe='')}/repository/files/{quote(path, safe='')}/raw"
        return path.rsplit("/", 1)[-1], api.stream(url, params={"ref": self._branch(api, pid)})

    def upload(self, api, folder_id, filename, path, size):
        if not folder_id:
            raise ConnectorError("Open a project (or a folder in it) to export to GitLab.")
        pid, folder = self._split(folder_id)
        branch = self._branch(api, pid)
        with open(path, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        for attempt in range(2):
            target = f"{folder}/{filename}".lstrip("/")
            resp = api.request("POST", f"{self.base}/api/v4/projects/{quote(pid, safe='')}/repository/files/{quote(target, safe='')}",
                               json={"branch": branch, "content": content, "encoding": "base64",
                                     "commit_message": f"Add {filename} from aiDocuMines"}, allow=(400,))
            if resp.status_code != 400:
                return {"id": f"path:{pid}:{target}", "name": filename}
            filename = _with_suffix(filename)          # 400: a file with that name exists
        raise ConnectorError("GitLab refused the file name.")


# ── Jira (Atlassian Cloud) ───────────────────────────────────────────────────

ATL = "https://api.atlassian.com"


class Jira(Provider):
    """Sites -> projects -> issues with attachments -> attachments. Exports attach the document to an issue."""
    key, name, category = "jira", "Jira", "Development"
    authorize_url = "https://auth.atlassian.com/authorize"
    token_url = "https://auth.atlassian.com/oauth/token"
    scopes = ["read:jira-work", "write:jira-work", "read:me", "offline_access"]
    client_id_env, client_secret_env = "ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET"
    pkce = False
    token_auth = "json"
    extra_auth_params = {"audience": "api.atlassian.com", "prompt": "consent"}
    export_hint = "Open an issue, then export: each document is attached to it."

    def account(self, api):
        return api.json("GET", f"{ATL}/me").get("email", "")

    def after_connect(self, api, tokens):
        sites = api.json("GET", f"{ATL}/oauth/token/accessible-resources")
        return {"sites": [{"id": s["id"], "name": s.get("name"), "url": s.get("url")} for s in sites]}

    def _jira(self, cloud: str) -> str:
        return f"{ATL}/ex/jira/{cloud}/rest/api/3"

    def list(self, api, folder_id, page_token):
        if not folder_id:
            sites = (api.account.extra or {}).get("sites") or self.after_connect(api, {})["sites"]
            return [item(f"site:{s['id']}", s.get("name") or s.get("url") or s["id"], "folder") for s in sites], None
        kind, *rest = folder_id.split(":")
        if kind == "site":
            start = int(page_token or 0)
            data = api.json("GET", f"{self._jira(rest[0])}/project/search", params={"startAt": start, "maxResults": 50})
            out = [item(f"project:{rest[0]}:{p['key']}", f"{p['key']} · {p['name']}", "folder") for p in data.get("values", [])]
            return out, None if data.get("isLast", True) else str(start + len(out))
        if kind == "project":
            params = {"jql": f'project = "{rest[1]}" AND attachments IS NOT EMPTY ORDER BY updated DESC',
                      "fields": "summary,attachment", "maxResults": 50}
            if page_token:
                params["nextPageToken"] = page_token
            data = api.json("GET", f"{self._jira(rest[0])}/search/jql", params=params)
            out = [item(f"issue:{rest[0]}:{i['key']}", f"{i['key']} · {(i.get('fields') or {}).get('summary', '')}", "folder")
                   for i in data.get("issues", [])]
            return out, data.get("nextPageToken")
        if kind == "issue":
            data = api.json("GET", f"{self._jira(rest[0])}/issue/{rest[1]}", params={"fields": "attachment"})
            atts = (data.get("fields") or {}).get("attachment") or []
            return [item(f"att:{rest[0]}:{a['id']}", a["filename"], "file", a.get("size"), a.get("created"), a.get("mimeType"))
                    for a in atts], None
        raise ConnectorError("Unknown Jira location.")

    def download(self, api, item_id):
        kind, cloud, att = (item_id.split(":") + ["", ""])[:3]
        if kind != "att":
            raise ConnectorError("Choose an attachment inside an issue.")
        meta = api.json("GET", f"{self._jira(cloud)}/attachment/{att}")
        return meta.get("filename", "attachment"), api.stream(f"{self._jira(cloud)}/attachment/content/{att}")

    def upload(self, api, folder_id, filename, path, size):
        kind, *rest = (folder_id or "").split(":")
        if kind != "issue":
            raise ConnectorError("Open an issue to attach documents to it.")
        with open(path, "rb") as fh:
            data = api.json("POST", f"{self._jira(rest[0])}/issue/{rest[1]}/attachments",
                            files={"file": (filename, fh)}, headers={"X-Atlassian-Token": "no-check"})
        first = data[0] if isinstance(data, list) and data else {}
        return {"id": f"att:{rest[0]}:{first.get('id')}", "name": first.get("filename", filename)}
