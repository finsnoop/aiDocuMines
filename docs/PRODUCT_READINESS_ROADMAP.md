# aiDocuMines: Product Readiness Roadmap

**Purpose:** the single guide for development until aiDocuMines is ready for production deployment and for public claims against competitors. Every piece of work should trace back to an item here.
**Baseline:** assessed from the code on 2026-11-20 (API `mvp_augment_branch` 927d2be, frontend `mvp_branch_augment_version` c67366d).
**Owner:** product and engineering leads. Update the status boxes as work lands; re-score the scorecard at the end of each phase.

---

## 1. Where we stand

aiDocuMines can credibly claim today to be **an AI-native document and matter workspace with contract intelligence**. It cannot yet claim full CLM, and it has no CRM.

| Area | What exists in the code | Benchmark leaders | Score* | Claim it? |
|---|---|---|---|---|
| **DMS** | Matters and folders, versions with version retention, sharing with password links, archive, OCR, keyword and semantic search (Elasticsearch, Milvus), in-browser Office editing (OnlyOffice), viewer for every format, multi-tenant RBAC, audit logs, connectors (Google Drive, Dropbox, OneDrive, Teams, Box, Slack, Notion, Salesforce, HubSpot, Zapier/Make) | iManage, NetDocuments, SharePoint/M365, M-Files, OpenText | ~70% | Yes, without "enterprise" |
| **CLM** | Templates and clause library (`document_automation`), drafting, redlining and compare, AI extraction of dates, values and risk, workflows with steps and assignments (`document_workflows`). Obligations (`PostCloseObligation`) and signature tracking (`SignatureTracker`) exist only in `private_equity`. | Ironclad, Icertis, DocuSign CLM, Agiloft, Juro | ~40% | As "contract intelligence" only |
| **CRM** | Salesforce and HubSpot connectors that bring documents in. No contacts, companies, relationships, pipeline or activities. | Salesforce, HubSpot, Dynamics; legal: InterAction, Clio Grow | ~5% | No |
| **AI on every object** | Ask Assistant on files, folders, matters, tables and figures; document agents with quoted evidence and verification; tabular review across documents (grid); any model through LiteLLM including local Ollama; anonymisation, DSAR and redaction; translation; AI gateway with spend by person and key, limits, and pricing from LiteLLM's registry | Harvey, Legora, CoCounsel, Luminance, Kira, Microsoft Copilot | Strong | Yes: this is the differentiator |

\* Scores are engineering judgement from the code, not a formal evaluation. Re-score at each phase gate.

### Positioning until the gaps close

> The AI-native document and matter workspace: every document, folder, matter and figure answers questions, with full control over models, data location and AI spend.

- Against iManage and NetDocuments: AI on every object, built in rather than bolted on.
- Against Harvey and Legora: a real document system underneath, with AI cost and data-residency governance.
- Say "contract intelligence", not CLM, until Phase 2 ships. Say "integrates with your CRM", not CRM.
- Do not compete on legal research over case law; we have no authoritative law corpus.

---

## 2. Production readiness gate

Nothing below is optional for production. The platform is **production-ready** when every box in this section is ticked.

### 2.1 Infrastructure and reliability
- [ ] Migrated off the single VPS to Azure (South Africa North), with no single point of failure for API, workers, database and storage
- [ ] Managed PostgreSQL with point-in-time recovery; Redis, Elasticsearch and Milvus with persistence and backups
- [ ] Backups restored successfully in a drill, with recovery time and data-loss targets written down and met
- [ ] Health checks, alerting and on-call; uptime target agreed (for example 99.9%) and measured
- [ ] Load test at 10x current volume: document open, search, upload and AI calls within agreed latency
- [ ] Infrastructure as code for every environment; staging mirrors production

### 2.2 Security and compliance
- [x] Inbound enterprise SSO: customers sign in with Entra ID (Azure AD), Okta or Google via SAML 2.0 or OIDC (2026-11-21: OIDC done in `custom_authentication/sso.py`, optional enforcement; SAML 2.0 still open)
- [x] SCIM user provisioning and deprovisioning (2026-11-21: SCIM 2.0 Users in `custom_authentication/scim.py`; deactivation ends sessions)
- [x] MFA enforceable per organisation (2026-11-21: "Require two-step sign-in" setting; emailed code at every password sign-in)
- [ ] Independent penetration test; all high and critical findings fixed
- [ ] SOC 2 Type I readiness (Type II and ISO 27001 to follow); policies, access reviews and evidence collection in place
- [ ] Encryption at rest and in transit documented; key management through Azure Key Vault; secrets rotated (including the Gmail app password)
- [ ] Data residency statement per tenant; data processing agreement and sub-processor list
- [ ] POPIA and GDPR: data-subject request flow (exists in `regulatory_compliance`) covered end to end, retention defaults documented

### 2.3 Records and governance (DMS enterprise layer)
- [x] Legal hold on documents and matters: held items cannot be edited, archived or deleted by anyone (2026-11-20: `core/holds.py`, Holds & walls page; API, folder and model-level guards; purge tools refuse while holds are active)
- [x] Retention and disposition schedules per document type and matter, with an approval step before destruction (2026-11-20: `core/retention.py`, Records & governance page; held and checked-out documents always kept)
- [x] Ethical walls: exclude named people or groups from a matter and everything in it, enforced in search, assistant and API (2026-11-20: `core/walls.py` in the central access layer; applies to every role)
- [x] Watermarking on view, download and share (2026-11-20: PDFs, organisation setting in Users & access; Office formats still to do)
- [x] Check-out and check-in locking for documents being edited (2026-11-20: `core/checkout.py`; editor read-only for others)
- [x] Immutable audit log export for every action, including AI calls (2026-11-21: hash-chained, append-only admin audit events; export of admin, sign-in, assistant, document and AI-call records with SHA-256; integrity check)

### 2.4 Quality bar
- [ ] All API and frontend test suites green in CI on every merge (today: 591 API, 243 frontend, run locally)
- [ ] End-to-end browser tests for the core journeys: upload to a matter, search, open, edit, share, archive and delete, ask the assistant, run OCR, translate and anonymise
- [ ] Accessibility: WCAG 2.1 AA on core pages
- [ ] Localisation framework in place (English first; structure for more languages)
- [x] Unused or broken code removed or fixed (2026-11-21: `ProjectsComponent` now renders the maintained projects table)

---

## 3. Phased roadmap

Each phase ends with a gate: re-score Section 1, update positioning, and only then start the next phase.

### Phase 1: Enterprise trust (weeks 1 to 6)
Goal: pass a law firm's security questionnaire.
- [ ] Azure migration with the reliability items in 2.1
- [x] SSO (OIDC; SAML open) and SCIM from 2.2
- [ ] Backup and restore drill
- [ ] Penetration test booked and remediated
- [ ] SOC 2 readiness programme started

**Done when:** a pilot customer's IT team signs off the security review and users sign in with their own identity provider.

### Phase 2: Where lawyers work, and the contract lifecycle (weeks 4 to 12)
Goal: close the biggest functional gaps against iManage, NetDocuments and Ironclad.
- [ ] **Outlook add-in:** file emails and attachments into matters through the shared ingestion pipeline (`core/ingest.py`), suggest the matter with AI, search the DMS from Outlook
- [ ] **Word add-in:** open from and save to the DMS, insert clauses, AI review in the side panel
- [x] **Contract record** with lifecycle stages: request, draft, review, approve, sign, active, renew or expire (2026-11-21: `contracts` app, Contracts page)
- [x] **Intake form** that starts a contract request and routes it (2026-11-21: requests queue for contract managers)
- [x] **Approval routing** (2026-11-21: rules by type, value, person or role; separation of duties; rejection returns to review)
- [ ] **E-signature:** DocuSign and Adobe Sign integrations first (native signing later)
- [x] **Obligations and renewals** for every organisation (2026-11-21: automatic renewal-notice and expiry obligations, recurrence, daily renewal/expiry run, email reminders; `import_pe_obligations` copies private-equity obligations; signatures follow with e-signature)
- [x] **Playbook review:** AI checks a contract against the organisation's positions and fallbacks and proposes redlines (2026-11-21: verbatim-quote verification, bounded prompts, AI gateway limits; Contracts, Playbooks)
- [x] Records and governance items from 2.3 (2026-11-21)

**Done when:** a contract goes from request to signature to renewal reminder without leaving the platform, and email filing from Outlook is in daily use by a pilot.

### Phase 3: Proof and differentiation (weeks 10 to 16)
Goal: make the AI claims measurable and add a feature competitors do not have together with the DMS.
- [ ] **AI evaluation suite:** a labelled test set; publish extraction accuracy per field, how often quoted evidence is correct, and assistant answer quality; run it on every model change
- [x] **Meter every AI call:** every DSPy call (private equity, IP litigation, regulatory compliance and others) now goes through the gateway's limits and the cost ledger (2026-11-20: `usage.instrument_dspy`)
- [ ] **One differentiator from Section 5**, chosen with sales: the virtual data room or relationship intelligence
- [ ] Mobile-friendly core pages (full mobile apps later)

**Done when:** accuracy numbers are on the website and the differentiator is used by a paying customer.

### Phase 4: Expansion (after production)
Pick from Section 5 by customer demand. Revisit CRM only when relationship intelligence shows pull.

---

## 4. Gap register

Use this table for tracking; link tickets in the last column.

| # | Gap | Area | Phase | Competitors that have it | Status | Ticket |
|---|---|---|---|---|---|---|
| G1 | Single VPS, no tested disaster recovery or uptime commitment | Infrastructure | 1 | All enterprise vendors | Open | |
| G2 | No inbound SSO (SAML/OIDC from customer identity providers) or SCIM | Security | 1 | iManage, NetDocuments, Ironclad, Harvey | Done 2026-11-21 (OIDC + SCIM; SAML open) | |
| G3 | No SOC 2 / ISO 27001, no pen test on record | Security | 1 | All enterprise vendors | Open | |
| G4 | No Outlook add-in for filing email into matters | DMS | 2 | iManage, NetDocuments | Open | |
| G5 | No Word add-in | DMS, CLM | 2 | iManage, NetDocuments, Ironclad, Spellbook | Open | |
| G6 | No legal hold, retention schedules, ethical walls, watermarking or check-out | DMS | 2 | iManage, NetDocuments, M-Files | Done 2026-11-20 (watermarks cover PDFs; Office formats open) | |
| G7 | No contract lifecycle stages, intake or approval routing | CLM | 2 | Ironclad, Icertis, Agiloft, Juro | Done 2026-11-21 | |
| G8 | No e-signature | CLM | 2 | DocuSign CLM, Ironclad, Juro | Open | |
| G9 | Obligations and signatures only in the private-equity module | CLM | 2 | Icertis, Ironclad | Obligations done 2026-11-21; signatures with G8 | |
| G10 | No playbook-based AI contract review | CLM, AI | 2 | Luminance, Spellbook, Ironclad | Done 2026-11-21 | |
| G11 | No published AI accuracy benchmarks | AI | 3 | Luminance, Kira, Harvey | Open | |
| G12 | Private-equity AI calls not metered by the AI gateway | AI | 3 | n/a | Done 2026-11-20 | |
| G13 | No CRM objects (contacts, companies, relationships, pipeline) | CRM | 4 | Salesforce, HubSpot, InterAction | Open | |
| G14 | No legal research corpus | AI | Not planned | Harvey, CoCounsel | Won't do | |
| G15 | No mobile apps; little localisation; accessibility incomplete | Reach | 2 to 3 | Most vendors | Open | |
| G16 | Unused, broken `ProjectsComponent` | Quality | 1 | n/a | Done 2026-11-21 | |

---

## 5. Additional capabilities to build on

Ranked by how much existing code each one reuses. Pick by customer demand once the production gate is passed (except where Phase 3 picks one).

| Capability | What it is | Builds on |
|---|---|---|
| **Relationship intelligence (CRM-lite)** | People and companies built automatically from documents, emails and connectors; who knows whom, matter history and contract value per counterparty | Document agents, `email_management`, Salesforce/HubSpot connectors |
| **Virtual data room (M&A)** | Per-bidder access, watermarking, Q&A threads, activity reports | Sharing and password links, `private_equity`, audit logs |
| **eDiscovery and litigation support** | Bates numbering, privilege logs, production sets | `class_actions` (`ExhibitPackage`), redaction, OCR |
| **Compliance and GRC** | Policy library, attestations, regulatory change tracking against policies | `regulatory_compliance` (DSAR, redaction), workflows |
| **HR and immigration documents** | Passports, visas, CVs, proof of address with expiry tracking | Document intelligence categories and expiry dates |
| **Entity management** | Entities, directors, filings and deadlines extracted from constitutional documents | Extraction, tasks |
| **Procurement and vendor risk** | Supplier contracts, obligations, spend and counterparty risk scores | CLM extraction, risk clauses |
| **Knowledge management** | Curated precedent bank; assistant drafts from the organisation's own precedents | Search, clause library, assistant |
| **Practice management** | Time captured from document and AI activity; e-billing review against billing guidelines | Activity logs, invoice extraction, `cost_centre` |
| **Client portal** | Clients upload, sign, track matter status and ask about their own documents | Sharing, matters, assistant |

---

## 6. Rules for every change until production

These apply to all three repositories (API, frontend, sign-in service).

1. **One way in for documents.** Every new source (add-ins, portals, connectors) uses the shared ingestion pipeline in `core/ingest.py`, so storage, the Locations tree and categorisation stay consistent.
2. **Every AI call goes through the AI gateway.** Record usage with `home_dash_analytics/agents/usage.py`, check limits with `gateway.enforce()`, and tag the key used. No unmetered model calls.
3. **AI on every object.** New objects (contracts, obligations, entities, contacts) get Ask Assistant and appear as data to the assistant, like existing pages.
4. **Multi-tenancy and RBAC first.** Every endpoint is scoped to the caller's organisation and checks a permission from `custom_authentication/rbac.py`. Add tests for cross-organisation access.
5. **No feature removal.** Redesigns keep every existing function and the existing look; tables keep sorting, filtering, search, resizing and pagination.
6. **Tests with every change.** API and frontend suites stay green; new features add tests, including permission and failure cases.
7. **No secrets in code or logs.** Production `.env` is never committed, printed or loaded on developer machines.
8. **Claims follow the scorecard.** Marketing and sales materials only claim what Section 1 marks as "Claim it: Yes".

---

## 7. Progress log

- **2026-11-20:** G12 closed (all DSPy calls metered and limited). G6 closed: legal holds, ethical walls, PDF watermarks, check-out locking, retention and disposition with approval. Also fixed: the document editor opened in edit mode for read-only users; admin settings treated the text "false" as on. Azure migration (G1) deliberately last, for cost.

- **2026-11-21:** G2 closed for OIDC (Entra ID, Okta, Google) with SCIM provisioning and enforceable SSO; two-step sign-in can be required per organisation; G16 closed; tamper-evident audit trail with export and verification.

- **2026-11-21:** CI runs on the working branches with frontend and Node unit tests; G7 closed, G9 obligations closed and G10 closed (Contracts page).

## 8. Review cadence

- **Weekly:** update the gap register status.
- **End of each phase:** re-score Section 1, revise positioning, confirm the next phase's scope.
- **Before production deployment:** every box in Section 2 ticked, signed off by engineering and product leads.
