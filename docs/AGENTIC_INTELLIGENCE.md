# Portfolio intelligence: implementation and release evidence

## Dashboard ownership

The existing Workflows page is the visual reference. Preserve its inherited Inter
font, warm neutral palette, orange accent, control sizing and table typography.
Existing widget components are reused, not replaced by differently styled copies.
`dashboard-audiences.ts` assigns every registered widget to exactly one dashboard;
a regression test rejects missing or duplicated assignments.

| Route | Audience | Contents |
|---|---|---|
| `/app/home` | GC, CEO, COO, CFO | Contract expiry and notice windows, evidenced financial amounts separated by currency, classifications, content, storage, privacy and recorded AI usage. Clicking a portfolio total opens its document evidence. |
| `/app/insights` | Managers, associates, directors and officers | Analysis runs, source selection, compliance rules, review queue, lexical/semantic comparisons, processing, workflow delivery and search metrics. Existing business reports remain available. |
| `/app/settings` | Tenant/platform administrators | Existing AI defaults, platform/organisation settings, security and compliance controls; advanced settings/indexing/audit; activity, endpoint health, user profile, billing, integrations and credentials metrics. Tenant administrators edit their own organisation, not global defaults. |

## Bounded orchestration

1. The authenticated actor chooses up to 20 documents through an ACL-filtered
   source catalogue. Organisation identity is derived from the token.
2. A persisted run contains actor, tenant and source IDs, never provider keys.
   Celery claims a queued run with a conditional database update; repeated task
   delivery cannot start a completed/claimed run again.
3. At execution, source reads go through `get_file_for_user(..., 'process')` and
   an additional tenant boundary. The worker reloads the actor's roles. Source
   permissions are checked again during extraction and before publication.
4. The configured organisation/platform model plans chunk order and field focus.
   Its plan must cover every section exactly once. It cannot name arbitrary
   tools, SQL statements, network addresses, credentials or write operations.
5. An extraction worker proposes typed facts. A separate verification pass can
   only retain proposed values, not introduce new ones. Programmatic validation
   rejects missing/nonmatching quotations, invalid dates, nonfinite/oversized
   amounts, invalid categories and scores. Conflicting section values are omitted.
6. Source hashes and update timestamps protect against stale results. A source
   changed during a run is rejected. A source changed afterwards is excluded from
   portfolio totals and marked for reanalysis.
7. Aggregation happens in PostgreSQL, not in an LLM. Financial totals group by
   currency; absence of an amount remains unknown. Evidence uses 50-row pages;
   overview requests do not load the full text of the portfolio.
8. Model calls record provider-reported token counts, provider/model/task,
   duration, prompt version and run/source IDs. No provider key enters the queue
   or API response. Unknown/missing usage is disclosed rather than invented.

Limits: 240,000 source characters per document, 12,000-character sections with
overlap, 100 recorded AI calls / 200,000 reported tokens per run, 30-minute hard
task limit, one active run per actor through the normal API. Provider requests
have timeouts; application-level provider retries are disabled apart from bounded
adaptation of unsupported model parameters. Oversized sources require splitting.
Failed/partial runs remain visible. A second model pass is additional checking,
not proof of correctness or independence from the generator's failure modes.

## Evidence and human authority

Dates, contract value, payment status, VAT amounts, interest rates, document
categories, risky clauses and compliance proposals carry exact source quotations
and character offsets. Risk exposure requires an explicit stated amount; a
potential termination loss is not fabricated from a model's intuition. AI scores
are not treated as probabilities or settled financial losses. Confidence is a
model estimate, not a calibrated accuracy guarantee.

Compliance checks use tenant-owned preset rules. With no rules, the agent cannot
publish a compliance result. Reviewing evidence requires document edit access.
Marking a finding reviewed does not approve an invoice or send a payment.

Uploaded and connector-imported `core.File` documents share the same ACL boundary.
This does **not** mean live OneDrive, Teams or SharePoint access exists: those
connectors still require implementation, credentials, tenant consent and testing.
OnlyOffice document editing is separate from Microsoft Graph integration.

## References and how they apply

All six requested references were read on 14 November 2026.

- [Anthropic: Building effective agents](https://www.anthropic.com/research/building-effective-agents):
  use composable orchestration, explicit tools, bounded stopping conditions and
  evaluator patterns. This implementation is a bounded orchestrator workflow,
  with model-directed planning inside fixed permission boundaries, not an
  unrestricted autonomous agent.
- [OpenAI: A practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/):
  keep tool capabilities narrow, reuse model configuration and retain human
  authority over sensitive actions. Payment execution is outside this layer.
- [OpenAI: Agentic governance cookbook](https://developers.openai.com/cookbook/examples/partners/agentic_governance_guide/agentic_governance_cookbook):
  version policy, guardrails and execution evidence alongside code. The page is
  marked archived, so its older API examples are not copied as current APIs.
- [Arthur: AI guardrails](https://www.arthur.ai/column/ai-guardrails-reduce-hallucinations):
  combine checks before and after model calls, provenance, abstention and
  deterministic arithmetic. Quote validation alone cannot prove entailment.
- [StackAI: Production hallucinations](https://www.stackai.com/insights/prevent-ai-agent-hallucinations-in-production-environments):
  distinguish unavailable sources from empty results, test failures, preserve
  traceability and gate releases on representative evaluation results.
- [NeuBird: Hallucination guardrails](https://neubird.ai/blog/ai-sre-hallucination-guardrails):
  constrain types and context size, verify outputs and test realistic failures.

Legal-tech research also informed notice deadlines and source-linked renewals:
[Ironclad vendor renewals](https://support.ironcladapp.com/hc/en-us/articles/40074088867863-Vendor-Renewals-View-on-the-Dashboard)
and [contract visibility](https://ironcladapp.com/resources/articles/contract-visibility).

## Release conditions still requiring evidence

Local unit/regression tests validate software controls, not legal/financial
extraction accuracy. This localhost stack currently has no reachable configured
AI provider. Before production release:

- Evaluate a human-labelled, permission-safe corpus across legal, immigration and
  finance, including conflicting amendments, multiple currencies, ambiguous dates,
  prompt injection and missing OCR. Measure field precision/recall, abstention,
  citation correctness, latency and cost by model/task; agree release thresholds.
- Route every legacy AI integration through consistent usage instrumentation.
  The current ledger reports recorded calls and identifies unattributed rows;
  it does not claim complete historical usage across uninstrumented integrations.
- Add durable document-level task fan-out, tenant concurrency quotas, cancellation,
  expired-run recovery and incremental refresh scheduling for large portfolios.
  Current bounded batches are not an unbounded portfolio ingestion service.
- Validate connector ingestion and revocation against each connected service.
- Review separate database enforcement (RLS/database-per-tenant), provider data
  processing agreements, backup/restore, secret rotation and deployment load tests.

Do not label these conditions completed merely because local tests pass.

An initial synthetic component evaluation is available once a provider is
configured:

```
python manage.py evaluate_dashboard_extraction --user admin@harperlowe.demo
```

It uses four fictional labelled cases, including an adversarial document, makes
real extraction/verification calls, accounts for those calls, and fails on
incorrect/unsupported fields or unavailable providers. It does not insert fictional
documents or findings into the tenant portfolio. This is a starting corpus, not
evidence of representative legal/financial accuracy; it has not been run against
a live model in this local stack.

Organisation integration credentials are encrypted at rest using the existing
AES-GCM field. Migration `system_settings.0003` converts legacy values and masks
credential entries in historical settings audit rows. Its data migration is
intentionally irreversible; retain the deployment backup and encryption key for
recovery rather than rolling credentials back into plaintext.
