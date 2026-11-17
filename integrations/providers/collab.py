"""Collaboration: Slack (channel files) and Notion (pages as Markdown)."""
from __future__ import annotations

import re

from .base import Api, ConnectorError, Provider, http_request, item

# ── Slack ────────────────────────────────────────────────────────────────────

SLACK = "https://slack.com/api"


def _slack(api: Api, method: str, http: str = "GET", **kw) -> dict:
    data = api.json(http, f"{SLACK}/{method}", **kw)
    if not data.get("ok"):
        err = data.get("error", "unknown_error")
        if err in ("invalid_auth", "token_revoked", "not_authed", "account_inactive"):
            raise ConnectorError("Slack no longer accepts this connection. Connect it again.")
        if err == "missing_scope":
            raise ConnectorError("The Slack connection is missing a permission. Connect it again.")
        raise ConnectorError(f"Slack could not complete that ({err}).")
    return data


class Slack(Provider):
    """Uses a user token: the files and channels the person can see in Slack, nothing more."""
    key, name, category = "slack", "Slack", "Collaboration"
    authorize_url = "https://slack.com/oauth/v2/authorize"
    token_url = f"{SLACK}/oauth.v2.access"
    scopes = ["files:read", "files:write", "channels:read", "groups:read", "users:read"]
    scope_param, scope_sep = "user_scope", ","
    client_id_env, client_secret_env = "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"
    pkce = False
    export_hint = "Open a channel, then export: the files are shared in that channel."

    def normalise_tokens(self, body):
        user = body.get("authed_user") or {}
        tokens = {"access_token": user.get("access_token"), "scope": user.get("scope"),
                  "refresh_token": user.get("refresh_token"), "team": (body.get("team") or {}).get("name")}
        if user.get("expires_in"):
            tokens["expires_in"] = user["expires_in"]
        if not tokens["access_token"]:
            raise ConnectorError("Slack did not grant access to your files. Try connecting again.")
        return tokens

    def account(self, api):
        data = _slack(api, "auth.test")
        return f"{data.get('user', '')} @ {data.get('team', '')}".strip(" @")

    def list(self, api, folder_id, page_token):
        if not folder_id:
            params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
            if page_token:
                params["cursor"] = page_token
            data = _slack(api, "conversations.list", params=params)
            channels = [item(f"channel:{c['id']}", f"#{c['name']}", "folder") for c in data.get("channels", [])
                        if c.get("is_member") or c.get("is_private")]
            mine = [item("mine", "My files", "folder")] if not page_token else []
            return mine + channels, (data.get("response_metadata") or {}).get("next_cursor") or None
        params = {"count": 100, "page": int(page_token or 1)}
        if folder_id == "mine":
            params["user"] = _slack(api, "auth.test").get("user_id")
        elif folder_id.startswith("channel:"):
            params["channel"] = folder_id.split(":", 1)[1]
        data = _slack(api, "files.list", params=params)
        files = [item(f["id"], f.get("name") or f.get("title"), "file", f.get("size"), str(f.get("timestamp", "")),
                      f.get("mimetype"), can_download=bool(f.get("url_private_download")))
                 for f in data.get("files", [])]
        paging = data.get("paging") or {}
        nxt = paging.get("page", 1) + 1
        return files, str(nxt) if nxt <= paging.get("pages", 1) else None

    def download(self, api, item_id):
        f = _slack(api, "files.info", params={"file": item_id}).get("file", {})
        url = f.get("url_private_download")
        if not url:
            raise ConnectorError("This Slack file cannot be downloaded (it may be a link or an external file).")
        return f.get("name") or f.get("title") or "slack-file", api.stream(url)

    def upload(self, api, folder_id, filename, path, size):
        if not (folder_id or "").startswith("channel:"):
            raise ConnectorError("Open a channel to export to Slack.")
        channel = folder_id.split(":", 1)[1]
        slot = _slack(api, "files.getUploadURLExternal", params={"filename": filename, "length": size})
        with open(path, "rb") as fh:
            resp = http_request("POST", slot["upload_url"], data=fh)
        if resp.status_code >= 400:
            raise ConnectorError(f"Slack did not accept the file ({resp.status_code}).")
        done = _slack(api, "files.completeUploadExternal", "POST",
                      json={"files": [{"id": slot["file_id"], "title": filename}], "channel_id": channel})
        return {"id": slot["file_id"], "name": ((done.get("files") or [{}])[0]).get("title", filename)}

    def revoke(self, api):
        api.request("GET", f"{SLACK}/auth.revoke", retry=False)


# ── Notion ───────────────────────────────────────────────────────────────────

NOTION = "https://api.notion.com/v1"
TEXT_BLOCKS = {"paragraph": "", "heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "quote": "> ",
               "bulleted_list_item": "- ", "numbered_list_item": "1. ", "to_do": "- [ ] ", "toggle": "", "callout": "> "}


def _plain(rich: list) -> str:
    return "".join(r.get("plain_text", "") for r in rich or [])


def _title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title")) or "Untitled"
    return "Untitled"


def blocks_to_markdown(blocks: list[dict]) -> str:
    lines = []
    for b in blocks:
        t = b.get("type")
        body = b.get(t) or {}
        if t in TEXT_BLOCKS:
            prefix = TEXT_BLOCKS[t]
            if t == "to_do" and body.get("checked"):
                prefix = "- [x] "
            lines.append(prefix + _plain(body.get("rich_text")))
        elif t == "code":
            lines.append(f"```{body.get('language', '')}\n{_plain(body.get('rich_text'))}\n```")
        elif t == "divider":
            lines.append("---")
        elif t == "child_page":
            lines.append(f"**{body.get('title', 'Untitled')}** (sub-page)")
        elif t in ("image", "file", "pdf", "video"):
            cap = _plain(body.get("caption"))
            lines.append(f"[{t}{': ' + cap if cap else ''}]")
        elif t == "table_row":
            lines.append("| " + " | ".join(_plain(c) for c in body.get("cells", [])) + " |")
    return "\n\n".join(lines).strip() + "\n"


def text_to_blocks(text: str) -> list[dict]:
    """Plain text into Notion paragraphs (2000 characters per text item, 100 blocks per request)."""
    blocks = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = para.strip()
        while para:
            piece, para = para[:2000], para[2000:]
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": [{"type": "text", "text": {"content": piece}}]}})
    return blocks or [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]


class Notion(Provider):
    """Pages the person shared with the integration. Import gives Markdown; export creates a child page."""
    key, name, category = "notion", "Notion", "Collaboration"
    authorize_url = "https://api.notion.com/v1/oauth/authorize"
    token_url = f"{NOTION}/oauth/token"
    scopes = []
    client_id_env, client_secret_env = "NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET"
    pkce = False
    token_auth = "basic"
    token_json = True
    extra_auth_params = {"owner": "user"}
    default_headers = {"Notion-Version": "2022-06-28"}
    export_hint = "Open a page, then export: each document becomes a sub-page with its text."

    def normalise_tokens(self, body):
        owner = ((body.get("owner") or {}).get("user") or {})
        return {**body, "label": (owner.get("person") or {}).get("email") or body.get("workspace_name", "")}

    def account(self, api):
        me = api.json("GET", f"{NOTION}/users/me")
        return (me.get("bot") or {}).get("workspace_name") or me.get("name", "")

    def list(self, api, folder_id, page_token):
        if not folder_id:
            body = {"filter": {"property": "object", "value": "page"}, "page_size": 100,
                    "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
            if page_token:
                body["start_cursor"] = page_token
            data = api.json("POST", f"{NOTION}/search", json=body)
            pages = [item(p["id"], _title(p), "folder", modified=p.get("last_edited_time")) for p in data.get("results", [])]
            return pages, data.get("next_cursor") if data.get("has_more") else None
        page = api.json("GET", f"{NOTION}/pages/{folder_id}")
        out = [item(folder_id, f"{_title(page)}.md", "file", modified=page.get("last_edited_time"), mime="text/markdown")]
        params = {"page_size": 100}
        if page_token:
            params["start_cursor"] = page_token
        data = api.json("GET", f"{NOTION}/blocks/{folder_id}/children", params=params)
        out += [item(b["id"], (b.get("child_page") or {}).get("title", "Untitled"), "folder")
                for b in data.get("results", []) if b.get("type") == "child_page"]
        return out, data.get("next_cursor") if data.get("has_more") else None

    def download(self, api, item_id):
        page = api.json("GET", f"{NOTION}/pages/{item_id}")
        blocks, cursor = [], None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = api.json("GET", f"{NOTION}/blocks/{item_id}/children", params=params)
            blocks += data.get("results", [])
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if not cursor or len(blocks) > 5000:
                break
        title = _title(page)
        text = f"# {title}\n\n" + blocks_to_markdown(blocks)
        safe = re.sub(r'[\\/:*?"<>|]+', "-", title)[:150]
        return f"{safe}.md", iter([text.encode("utf-8")])

    def upload(self, api, folder_id, filename, path, size, text: str | None = None):
        if not folder_id:
            raise ConnectorError("Open a Notion page to export into it.")
        if text is None:
            raise ConnectorError("Only documents with readable text can be exported to Notion.")
        blocks = text_to_blocks(text)
        page = api.json("POST", f"{NOTION}/pages", json={
            "parent": {"page_id": folder_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": filename[:200]}}]}},
            "children": blocks[:100]})
        for i in range(100, len(blocks), 100):
            api.json("PATCH", f"{NOTION}/blocks/{page['id']}/children", json={"children": blocks[i:i + 100]})
        return {"id": page.get("id"), "name": filename}
