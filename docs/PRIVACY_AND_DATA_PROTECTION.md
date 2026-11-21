# aiDocuMines: privacy and data protection

**For:** customers' security, privacy and procurement teams, and Datasqan staff answering their questionnaires.
**Covers:** roadmap section 2.2 (encryption, key management, data residency, sub-processors, POPIA/GDPR data-subject
requests, retention). Every statement here describes the code as of 2026-11-21; file references let an auditor check it.

> **Before sending to a customer:** fill in the items marked **[confirm]**, and have a lawyer review section 8 (the
> data processing agreement outline). This document describes how the platform works; it is not legal advice.

---

## 1. Roles

| Party | Role (POPIA / GDPR) |
|---|---|
| The customer organisation | **Responsible party / controller** for its documents and the personal data in them |
| Datasqan (operator of aiDocuMines) | **Operator / processor**: processes that data only to provide the service, on the customer's instructions |
| Services in section 5 | **Sub-operators / sub-processors**, each used only for the purpose stated |

## 2. Where data is stored

| | Today | After the planned move to Azure |
|---|---|---|
| Hosting | Single virtual server at **[confirm: provider and country]** | Microsoft Azure, **South Africa North** (`infra/azure/terraform`) |
| Database | PostgreSQL on the same server | Azure Database for PostgreSQL Flexible Server, private network; 14 days point-in-time recovery with geo-redundant backup copies (Azure's paired region for South Africa North is South Africa West); zone-redundant standby optional |
| Documents | Server disk | Azure Files (ZRS), private network |
| Search indexes | Elasticsearch and Milvus on the server | Private VM in the same region |
| Backups | **[confirm: location and schedule]** | Recovery Services vault in the same region |

Each organisation's administrators see a live statement for their own organisation under
**Administration → Settings → Where your data is** (`/api/v1/auth/admin/data-residency/`): storage provider and
region (from `DATA_HOSTING_PROVIDER`, `DATA_HOSTING_REGION`, `DATA_BACKUP_REGION`), the AI provider and models their
organisation uses, and every other service their organisation has actually connected (e-signature, connectors, email,
web search). Set those three variables on the server so the statement is complete.

## 3. Encryption

| Where | How | Code |
|---|---|---|
| Browser ↔ platform | HTTPS; HSTS (180 days by default); secure cookies | `aiDocuMines/settings.py` (`SECURE_HSTS_SECONDS`, `SESSION_COOKIE_SECURE`) |
| Platform ↔ database | TLS required by default (`POSTGRES_SSLMODE=require`; `verify-full` with a CA path) | `settings.py`, `deploy/postgres-tls-entrypoint.sh` |
| Secrets in the database | AES-256-GCM per value: organisation AI keys, SSO client secrets, connector tokens, integration credentials | `core/fields/encrypted_char.py` (key `ENCRYPTION_SECRET_KEY`) |
| Passwords | Never stored; one-way hashes (Django's PBKDF2) | Django auth |
| Documents and database at rest | Disk encryption of the hosting provider | Today **[confirm]**; on Azure, platform-managed encryption for Files, PostgreSQL and disks (on by default) |
| Signed links | Editor downloads and share links are signed and expire | `core/onlyoffice_utils.py`, `document_operations` |
| Audit trail | Append-only, each event hash-chained to the previous one; export carries a SHA-256 checksum; integrity check in the admin console | `custom_authentication/models.py` (`AdminAuditEvent`), `views_audit_export.py` |

## 4. Key management and rotation

| Key | Protects | Today | On Azure |
|---|---|---|---|
| `ENCRYPTION_SECRET_KEY` | All encrypted database values (section 3) | Server `.env`, never committed or logged | Key Vault secret, read by managed identity |
| `FIELD_ENCRYPTION_KEY` | Fernet-encrypted fields (library default; none in active use) | Server `.env` | Key Vault |
| `SECRET_KEY` | Signed links and sessions | Server `.env` | Key Vault |
| OIDC signing key | Tokens issued by the platform | `oidc-private-key.pem` on the server | Key Vault |
| Provider keys (AI, e-signature, email) | Calls to those services | Server `.env` / organisation settings (encrypted) | Key Vault / organisation settings |

**Rotation** (tested in `core/tests/test_key_rotation.py`): set the key to `"<new>,<old>"`, restart, run
`python manage.py rotate_encryption_keys` (first with `--dry-run`), and when it reports 0 unreadable values, set the key
to `"<new>"` and restart. The command re-encrypts every encrypted column and never double-encrypts a value it cannot read.

**Open action:** rotate the Gmail app password used for outgoing email, and move it to Key Vault with the migration.

## 5. Sub-processors

Only services that are configured on the server, or that an organisation itself connects, receive any data.

| Service | Purpose | Data sent | When |
|---|---|---|---|
| Hosting provider (**[confirm]**; Microsoft Azure after migration) | Runs the platform | Everything stored | Always |
| AI model providers via the LiteLLM gateway: OpenAI, Anthropic, Google (Gemini), OpenRouter, or local Ollama/vLLM | Assistant, document intelligence, playbook review, translation and other AI features | The question and the relevant document passages for each request | When the organisation uses AI features; the organisation chooses the provider, the allowed models, or local models only (Administration → Settings) |
| Google (Gmail SMTP) | Sign-in codes, password resets, notifications | Email address, message text | Always (outgoing email) |
| Web search: Tavily, Brave or Serper (one, set by `WEB_SEARCH_PROVIDER`) | Assistant "search the web" | The question text only | Only when web search is enabled for the organisation and a person asks for it |
| DocuSign / Adobe Acrobat Sign | Electronic signature | The document sent for signature and signer names/emails | Only when the organisation connects its own account and sends a document |
| Microsoft 365 (Outlook and Word add-ins) | Filing email, editing in Word | Content the person chooses to file or save | Only when the organisation deploys the add-ins |
| Google Drive, Dropbox, OneDrive, Teams, Box, Slack, Notion, Salesforce, HubSpot, Zapier, Make | Import/export connectors | Documents the person chooses to move | Only when a person links that service |
| Stripe | Subscription billing | Billing contact and plan | Only for paid subscriptions **[confirm in use]** |

The AI gateway records which model, provider and key handled every call (never the content), with spend limits per
person and organisation (`home_dash_analytics/agents/gateway.py`).

## 6. Data-subject requests (POPIA ss 23-25, GDPR arts 15-21)

**About people who use the platform** (`custom_authentication/privacy.py`, tests in `tests_privacy.py`):

| Right | How |
|---|---|
| Access and portability | The person: **Profile & security → Your data → Download my data**. An administrator: person drawer → **Export personal data**. Machine-readable JSON: profile, roles, activity, assistant questions, AI usage, documents owned (listed; contents belong to the organisation). Secrets are never included. |
| Correction | The person edits their profile; administrators edit name and roles. |
| Erasure | Administrator: person drawer → **Erase personal data**, confirmed by typing the email. Removes name, contact details, address, preferences, password, two-step secret, API keys, sessions, assistant questions and activity details; the account becomes `erased-<id>@erased.invalid` and is disabled. Documents stay with the organisation (optionally moved to a colleague; documents under legal hold stay put). Refused for the organisation's last administrator and for yourself. |
| Kept after erasure, and why | The administrative audit trail (tamper-evident; changing it would destroy its value as evidence) and AI usage amounts for billing: kept under the exemptions for legal obligations and legal claims (POPIA s 14(1); GDPR art 17(3)(b) and (e)). The erasure itself is audited without the email. |

Deleting an account outright is refused while the person owns documents, because it would delete those documents
without the organisation's retention and disposition approval; erasure is used instead.

**About people named inside documents:** the organisation (as responsible party) uses the regulatory-compliance tools:
DSAR requests with search across its documents (`regulatory_compliance`, `DSARRequest`), redaction and anonymisation,
legal holds and retention schedules with approved disposition (`core/holds.py`, `core/retention.py`).

## 7. Retention defaults

| Data | Default | Where set |
|---|---|---|
| Documents, versions | Kept until the organisation's own retention schedule disposes of them, with an approval step; never while on legal hold | Records & governance page (`core/retention.py`) |
| Sign-in and activity records | **730 days** | `RETENTION_ACTIVITY_DAYS` (minimum 30) |
| Assistant and grid questions and answers | **730 days** | `RETENTION_ASSISTANT_DAYS` (minimum 30) |
| Expired access tokens, password-reset tokens, sign-in codes | 1 day after expiry | `core/maintenance.py` |
| Administrative audit trail | Kept (append-only); export for archive | `AdminAuditEvent` |
| AI usage and cost records (amounts, model, key; no content) | Kept for billing and audit | `cost_centre` |
| Backups | **[confirm]** today; on Azure, 14 days point-in-time for PostgreSQL (`postgres.backup_retention_days` in `infra/azure/terraform/variables.tf`) | |

Retention runs daily at 03:37 (`core.maintenance.apply_privacy_retention`).

## 8. Data processing agreement: outline for legal review

1. Subject matter, duration, nature and purpose of processing: providing aiDocuMines to the customer.
2. Types of personal data and data subjects: users' account data; any personal data in the customer's documents.
3. Processing only on documented instructions; confidentiality of personnel.
4. Security measures: sections 3, 4 and the platform's access controls (multi-tenant isolation, roles, ethical walls,
   legal holds, audit trail, SSO/SCIM, required two-step sign-in).
5. Sub-processors: section 5; notice of changes and the right to object.
6. Assistance with data-subject requests (section 6) and with security incidents, including notification without undue delay.
7. Deletion or return at the end of the service (export, then deletion of the organisation's data and backups by
   the backup expiry date).
8. Audits and information: this document, the audit-trail export, penetration-test summary (roadmap 2.2), SOC 2 when available.
9. Cross-border transfers: hosting region (section 2); AI providers outside South Africa process request content under
   their terms; organisations that need in-country processing can choose local models (Ollama/vLLM).
