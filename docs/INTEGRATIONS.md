# Integrations: setting up each connector

Every connector on **Integrations** (`/app/integrations`) lets a user connect their own account, browse it,
**import** documents into a matter, and **export** aiDocuMines documents back. Imports go through the same path
as *Matters → New matter → Upload* (`core/ingest.py`): same storage layout, folder tree, duplicate handling,
upload policy and processing (text extraction, indexing, classification, anonymisation).

A connector shows **Setup needed** until an administrator adds its app keys to the API's `.env` and restarts the API.
Administrators see the exact variable names and the redirect URL on the connector's page.

## Common settings

| Variable | Purpose |
|---|---|
| `INTEGRATIONS_PUBLIC_API_URL` | Public base URL of the API, e.g. `https://aidocumines-api-layer.apps.datasqan.com`. Used for OAuth redirect URLs and Zapier/Make upload URLs. Set it whenever the API is behind a proxy. |
| `FRONTEND_URL` | Where users return after signing in to a provider (`/app/integrations`). |
| `CONNECTOR_MAX_IMPORT_MB` | Largest single file a connector import accepts (default 500). |
| `CONNECTOR_INBOUND_MAX_MB` | Largest file Zapier / Make may send in (default 100). |

**Redirect URL** for every OAuth app: `<INTEGRATIONS_PUBLIC_API_URL>/api/v1/integrations/<service>/auth/callback/`

## Providers

| Connector | Service id | Register the app at | `.env` keys | Scopes / permissions to grant |
|---|---|---|---|---|
| Google Drive | `google-drive` | Google Cloud Console → APIs & Services → Credentials (OAuth client, Web) + enable Drive API | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | `openid email drive.readonly drive.file` |
| Dropbox | `dropbox` | dropbox.com/developers/apps (Scoped access, Full Dropbox) | `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET` | `account_info.read files.metadata.read files.content.read files.content.write` |
| Microsoft OneDrive | `onedrive` | Microsoft Entra ID → App registrations (Web redirect) | `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, optional `MICROSOFT_TENANT` (default `common`) | Delegated: `offline_access User.Read Files.ReadWrite` |
| Microsoft Teams | `microsoft-teams` | same app registration as OneDrive (add its redirect URL too) | same as OneDrive | Delegated: `Team.ReadBasic.All Channel.ReadBasic.All Files.ReadWrite.All` (admin consent may be required) |
| Box | `box` | developer.box.com → Custom App, User Authentication (OAuth 2.0) | `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET` | "Read and write all files and folders" |
| Slack | `slack` | api.slack.com/apps → OAuth & Permissions → **User** token scopes | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` | `files:read files:write channels:read groups:read users:read` |
| Notion | `notion` | notion.so/my-integrations → Public integration | `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET` | Read, update and insert content. Users choose which pages to share when connecting. |
| GitHub | `github` | GitHub → Settings → Developer settings → OAuth Apps | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | `repo read:user user:email` |
| GitLab | `gitlab` | GitLab → Preferences → Applications (or group/instance applications) | `GITLAB_CLIENT_ID`, `GITLAB_CLIENT_SECRET`, optional `GITLAB_URL` for self-hosted | `read_user api` |
| Jira | `jira` | developer.atlassian.com → OAuth 2.0 (3LO) integration | `ATLASSIAN_CLIENT_ID`, `ATLASSIAN_CLIENT_SECRET` | `read:jira-work write:jira-work read:me offline_access` |
| Salesforce | `salesforce` | Setup → App Manager → New Connected App (OAuth, PKCE) | `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET`, optional `SALESFORCE_LOGIN_URL` (sandbox: `https://test.salesforce.com`) | `api refresh_token` |
| HubSpot | `hubspot` | developers.hubspot.com → App → Auth | `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` | `oauth files` |
| Zapier | `zapier` | nothing to register | none | The user pastes a *Catch Hook* address and chooses a default matter. |
| Make | `make` | nothing to register | none | The user pastes a *Custom webhook* address and chooses a default matter. |

## What each connector browses

| Connector | Import from | Export to |
|---|---|---|
| Google Drive, Dropbox, OneDrive, Box | folders and files (Google Docs/Sheets/Slides import as Word/Excel/PowerPoint) | the open folder |
| Microsoft Teams | teams → channels → channel files | the open channel or folder |
| Slack | "My files" and channels → files | the open channel |
| Notion | shared pages (as Markdown) and sub-pages | a new sub-page of the open page, with the document's text |
| GitHub, GitLab | repositories / projects → files | a commit in the open folder (default branch); an existing name gets a timestamp |
| Jira | sites → projects → issues with attachments → attachments | an attachment on the open issue |
| Salesforce | Files (newest first) | a new File |
| HubSpot | file manager folders and files | the open folder (private files) |

## Zapier and Make

* **Events out**: `document.uploaded`, `import.completed`, `export.completed` (and `test`), POSTed as JSON to the
  webhook with `X-aiDocuMines-Signature: sha256=<HMAC-SHA256 of the body with the signing secret>`.
* **Documents in**: `POST <upload URL>` with a multipart `file`, or JSON `{"file_url": "https://…"}`; optional
  `matter_id` (otherwise the connection's default matter) and `filename`.
* Only public `https://` addresses are accepted in either direction (no internal or private networks).

## Security notes

* Tokens are encrypted at rest (`ENCRYPTION_SECRET_KEY`) and never returned by the API.
* Each sign-in uses a single-use `state` bound to the user who started it; PKCE where the provider supports it.
* Imports and exports run in the background (fast worker lane) and only touch documents the user may open.
