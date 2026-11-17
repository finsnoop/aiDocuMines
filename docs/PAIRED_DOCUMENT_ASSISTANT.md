# Ask the Assistant: two-document intelligence

The Workspace and Drafting buttons open a dedicated document-pair Assistant.
This endpoint does not use dashboard metrics as document context.

## Behaviour

- The catalogue contains 100 versioned presets: 12 primary actions and 88 questions
  in the supplied categories. Selecting a preset executes it against the current
  pair; opening the panel does not run 100 model requests.
- Original / Processed / Edited describe panes, not document ancestry. The model
  classifies the pair conservatively. Version-change and transformation-fidelity
  questions abstain when the required relationship cannot be established.
- Each analysis uses the configured organisation/platform AI provider, with a
  generation pass and a narrower verification pass. File ACLs, tenant membership,
  actor activity and Assistant permission are rechecked before publication.
- Stored sources contain current saved/extracted text and separately identified
  file metadata. Browser-generated text, HTML and JSON are accepted as explicitly
  caller-provided Processed content; that provenance is not independently verified.
  Browser-only PDF/Office results must first be saved or uploaded as stored files.
- Model citations must quote an exact supplied passage or metadata excerpt. The
  API returns pane, file, quotation and character offsets; it never invents page
  numbers. Findings distinguish facts, comparisons, inferences and questions.
- The arithmetic tool uses sourced literal operands, an operation allowlist and
  Decimal. It cannot execute code, SQL, URLs, writes or payment operations.
- A five-minute answer cache is scoped to actor, tenant, document fingerprints,
  question, history, model and prompt version. ACL/content checks run before every
  cache hit. Provider usage is recorded through the existing AI usage ledger.
- Responses disclose sampled/partial document coverage, uncertain relationships
  and missing evidence. Model verification and exact quote checks do not establish
  that every inference is correct; domain review remains necessary.

## API

`GET /api/v1/core/assistant/pair/presets/` returns the catalogue.

`POST /api/v1/core/assistant/pair/ask/` accepts:

```json
{
  "original_file_id": 101,
  "processed_file_id": 102,
  "processed_label": "Edited",
  "preset_id": "p02",
  "question": "Optional additional context",
  "history": []
}
```

For a generated preview, replace `processed_file_id` with `processed_inline`:
`{"content":"...","format":"text|html|json","filename":"Results"}`.
The original always requires a readable stored file. Sources from inline content
have a null file ID. No source URL or filesystem path is accepted from the caller.

Results include relationship, source references, typed findings, limitations,
verified calculations, coverage, suggested follow-ups and cache/model metadata.
Unknown or inaccessible file IDs return 404; invalid requests return 400; Assistant
policy refusals return 403; provider failures return a clean 502.

## Viewing evidence and saved edits

Clicking evidence closes the drawer, reveals/focuses the corresponding pane and
shows its supporting quotation. Accessible in-page document text is selected when
the viewer exposes a matching passage. Native PDF and cross-origin OnlyOffice
viewers do not expose a universal cursor/highlight API; the quotation remains
visible for their Find control. Metadata references are clearly distinguished
from document-body passages. This version does not claim automatic Office cursor
or PDF page navigation when the viewer has not provided those locations.

OnlyOffice analyses use saved content, not unsaved editor changes. Saving now
invalidates old extracted text and advances the document update timestamp.
Migration `core.0015` discards potentially stale derived text for legacy `Saved`
documents; it retains the actual files. On-demand extraction uses a conditional
write so a concurrent editor save cannot restore stale extracted text.

## Deployment and validation

No additional package or AI credential is needed. Existing server environment and
organisation model settings supply LiteLLM, Ollama, vLLM or OpenAI configuration.
Deploy the API before the frontend; the normal API restart script applies the
data migration. The frontend and API commits must both be pulled.

Focused regression checks cover tenant/ACL isolation, cache revocation and source
changes, malformed requests/model results, quote validation, applicability,
arithmetic, inline provenance, table-cell boundaries and editor-save races.
Frontend checks cover pane identity, generated artifact IDs, stale selection and
request cancellation, presets, evidence rendering and output escaping.
Production builds validate integration with both existing toolbars.

The local tests use controlled model responses. Live model accuracy, latency and
real PDF/Office evidence navigation must also be checked on the deployed server
using the configured providers and representative tenant-approved documents.
