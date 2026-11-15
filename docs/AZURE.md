# aiDocuMines on Azure: sizing, load test and cost basis

Target: 5 organisations × 500 users (2,500 users), Azure South Africa North, Container Apps.
Infrastructure as code: `infra/azure/terraform` (validated against azurerm 5.14.0). Cost model:
`infra/azure/pricing` (re-runnable against Microsoft's price API). Test tooling: `scripts/scale/`.

## 1. How it was measured

| Step | Tool | Data |
|---|---|---|
| Seed | `manage.py seed_scale --tenants 5 --users 500 --files-per-user 8 --matters 25` | 2,500 users, 20,000 documents, 125 matters, ~10,000 shares; each organisation's data carries a unique marker |
| Isolation | `scripts/scale/isolation_sweep.py` | Every GET route (261 without IDs, 63 with IDs) as Admin / Manager / Auditor / member of each organisation; IDs of other organisations' objects substituted into every route with IDs; responses scanned for other organisations' markers |
| Load | `scripts/scale/locustfile.py` (Locust 2.37.11) | Real tokens; browser flow (open app, Home, documents table and search, folders, shared, matters, workflows, Insights, assistant history); 2-8 s pause between clicks; 100 / 300 / 600 simultaneous users, 3 minutes each |
| Server | gunicorn gthread 2 workers × 8 threads, limited to **1 vCPU / 2 GiB** | One Container Apps replica of the size used in Terraform |

## 2. Results

**Isolation:** 0 responses with another organisation's data (10,780 calls, original code; repeated on the
final code, see section 6).

**Throughput of one 1-vCPU replica** (warm; 300 and 600 users):

| | Before fixes | After fixes |
|---|---|---|
| Requests/s at 300 users | 16.4 | 24.5 |
| Requests/s at 600 users (saturated) | 23.0 | 27.4 |
| 95th percentile at 300 users | 33 s | 16 s |
| Database CPU | 95-117 % | 26-59 % |
| Errors | 449 (test artefacts + 1 real) | 0 |

The 300/600-user latencies are queueing on a single saturated vCPU: the test users click every 2-8 s,
far more than real users. They show capacity, not the latency people will see with autoscaling.

**Planning figures**
- One 1-vCPU API replica: ~25 req/s saturated; **~16 req/s at a comfortable ~65 % utilisation**.
- Database: **~2 % of a vCore per req/s** (local PostgreSQL 16, same queries).

## 3. Fixes made because of the test

| Problem found | Fix |
|---|---|
| Sign-in throttled per IP: an office of 500 behind one address locks itself out | Per account (+ address) limit with a 600/min per-address ceiling; same in the Node service; tests |
| Every listing ran `SELECT DISTINCT` over all `core_file` columns incl. document text | Sharing rule rewritten as per-user ID lookups (indexed); no DISTINCT unless the caller's joins need it. Documents table 86 → 40 ms, its queries 36+27 → 5+2 ms |
| Folder tree: ~6 queries per folder | Whole tree preloaded in 11 queries; response byte-identical |
| Admin user list: ~1,000 queries for 500 users | 4 queries |
| Home: live call to the AI provider's model list on every load; waits out timeouts when it is down | Model lists cached in Redis (10 min; failures 60 s); capacity report kept warm by the scheduler |
| Anonymiser entity list: 89 s and ~1.5 GB per web process | Answered from cache / taxonomy; exact list computed once by a worker |
| ~30 service endpoints required `X-Client-Secret`, which the browser (rightly) no longer has: OCR, translation, anonymisation, redlining, versioning, workflows, clustering, Insights analytics answered 400 | Identity from the bearer token; guard test fails if any service requires a client secret |
| `cost/subscriptions/me`: only the first user on the platform could open it | Empty Stripe ID stored as NULL (unique column) |
| Compliance alert detail endpoints never worked; connection test crashed on a malformed id | Fixed (404 / single-alert lookup) |

## 4. Sizing for 2,500 users (assumptions to verify)

**Assumption (verify with production's `UserAPICall` log before go-live):** at peak 40 % of users are active
(1,000 people), each causing about one API request every 10 s (a page every ~30 s, ~3 API calls).
That is **~100 req/s at peak**.

| Component | Need at 100 req/s | Terraform default |
|---|---|---|
| API (1 vCPU / 2 GiB) | 100 / 16 ≈ 7 replicas at peak | min 2, max 10, scale at 40 concurrent requests |
| Database | 100 × 2 % ≈ 2 vCores busy | GP D4ds_v5 (4 vCores) + PgBouncer |
| Redis | queue + cache are small | Managed Redis Balanced B1, HA |
| AI workers | depends on uploads, not users | D4 profile, 1-3 nodes, scale on queue length |

**Sensitivity**

| Peak load | API replicas at peak | Database |
|---|---|---|
| 50 req/s | 4 | D2ds_v5 would do |
| 100 req/s (assumed) | 7 | D4ds_v5 |
| 200 req/s | 13 (raise `max_replicas`) | D8ds_v5 |

Average API replicas over a month (10 busy hours × 22 working days at ~5, otherwise 2): **≈ 3**.

## 5. Monthly cost (USD, pay-as-you-go, South Africa North)

Run `python infra/azure/pricing/cost_model.py <prices folder> --zar <rate>` for every line with SKU, quantity,
unit price, formula and the exact Azure Retail Prices API query. Summary at the defaults above:

| Group | USD / month |
|---|---|
| Container Apps, Consumption (API, frontend, Node, fast worker, beat, file monitor, LiteLLM, requests) | 569.45 |
| Container Apps, Dedicated D4 ×2 avg (AI worker, OnlyOffice, Ollama) + plan management | 522.62 |
| Front Door Premium + WAF (Zone 7: Middle East & Africa) | 413.60 |
| PostgreSQL Flexible Server GP D4ds_v5, 128 GB, backups | 376.89 |
| Search VM D4s_v5 + Premium SSD (Elasticsearch, Milvus) | 267.33 |
| Registry, Key Vault, Log Analytics, private endpoints, egress | 239.98 |
| Azure Managed Redis Balanced B1 | 146.73 |
| Azure Files Hot ZRS (500 GB documents) | 82.56 |
| Backup (search VM, documents share) | 59.64 |
| **Total** | **2,678.80** |

Not included: VAT, Azure support plans, Defender for Cloud (opt-in), AI model usage (tokens; tracked
in-app by the cost agent), a separate staging environment, database zone-redundant HA (+~$377/month).

**Sources:** unit prices from the [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
(queried 2026-11-15, `armRegionName eq 'southafricanorth'`, Front Door `Zone 7`); billing rules from
[Container Apps billing](https://learn.microsoft.com/azure/container-apps/billing) (free grants, idle rates,
plan management) and [Front Door billing](https://learn.microsoft.com/azure/frontdoor/billing) (Premium
includes managed WAF rules and Private Link origins); Zone 7 = Middle East and Africa per the
[Front Door pricing page](https://azure.microsoft.com/pricing/details/frontdoor/).

## 6. Before go-live

- Re-run the load test against the Azure staging environment (`locust -f scripts/scale/locustfile.py --host https://<staging api>`).
- Verify the peak-load assumption from production logs; adjust `api.max_replicas` and the database SKU.
- Remaining per-request cost is Django/DRF overhead (~37 ms CPU per request); next candidates: caching
  Home's business view per user for 60 s, token lookup cached per request.
