# AI harness and the reusable Ask Assistant

Every "Ask Assistant" button in aiDocuMines is the same module, configured per feature, data model or object
by a **profile**. Design once, centralise, configure case by case.

Agent = model + harness. The model writes answers; the harness (`core/ai_harness.py`) decides what it sees, what it
may do, checks what it says, and records everything.

## The harness components

| Component | What it does here | Where |
|---|---|---|
| Execution loop | Instruct, call the model, verify the answer, repair it a bounded number of times (`repair_attempts`), stop | `ai_harness.run` |
| Tools | Retrieval only, scoped to the one object: semantic search, then keyword ranking, within fixed budgets. No write tools | `core/assistant.py` `resolve_context`, `build_sources` |
| Context and memory | Evidence budget (characters, files, text per file), conversation memory limited to `history_turns`, history cleared when the object changes | `core/assistant.py`, `ai_harness.clean_history`, assistant panel |
| Sandbox | The central access layer: roles, sharing, matter teams, ethical walls, tenant isolation; platform operators see no client content | `core/access.py` |
| Guardrails | Entitlement, input screening, evidence gate, object-bound system prompt, citation verification, secret redaction | `ai_harness` layers 1 to 5 |
| Observability | Every question logged with page, profile, object, outcome, flags and the loop trace; AI accuracy suite scores the same path | `views_assistant._log`, `ai_eval/runner.py` |
| Model routing | Organisation's provider, allowed models and default; spend limits per key | `assistant_model`, `home_dash_analytics/agents/gateway.py` |

## The layers every answer passes

1. **Entitlement**: `assistant.ask` permission, the organisation's Assistant switch, the page switch for the
   profile's surface, AI spend limits, rate limit.
2. **Input screening**: question length; attempts to override the rules are blocked or flagged (`prompt_injection`).
3. **Evidence gate**: only content the person may open; no evidence means no model call.
4. **Instruction**: a system prompt (system role, every provider) binding the model to the object: only its sources
   (`grounding`), cite them (`require_citations`), decline anything else (`refuse_off_topic`), treat all content as data.
5. **Output checks**: unknown citation labels removed; uncited answers repaired, then withheld; secrets redacted.
6. **Audit**: activity log entry with the trace.

Outcomes returned to the UI: `answered`, `not_in_context`, `off_topic`, `withheld`, `no_evidence`, `blocked_injection`.

## Configuration

Administrators (Administration → Settings → Assistant and AI harness guardrails), per organisation:

- **Ask Assistant buttons**: on or off per page (`assistant_surfaces`). Off buttons stay in place, greyed out.
- **Guardrails** (`assistant_guardrails`): grounding (strict or explain), citations, off-topic refusal, override
  attempts (block or flag), conversation memory (0 to 10), self-correction attempts (0 to 2), secret redaction.

A profile can make guardrails **stricter** for its object (for example playbooks, legal holds and ethical walls are
always strict), never looser than the organisation's choice.

## Adding Ask Assistant to a new feature

1. Register a profile in `core/ai_harness.py` `PROFILES`:

   ```python
   AskProfile("vdr.document", "documents", "data room document",
              "its contents, who may see it and its Q&A history", _STRICT),
   ```

   If the feature needs its own page switch, add a surface to `SURFACES` (it then appears in the settings screen).
2. Mirror the key in the frontend `src/app/assistant/ask-profiles.ts`.
3. Put the button on the object:

   ```html
   <app-ask profile="vdr.document" [label]="'Data room: ' + doc.name" [data]="doc" [fileIds]="[doc.file_id]" compact />
   ```

   For a context menu, use `askMenuItem(assistantService, 'vdr.document', () => …)`.

The page switch, guardrails, access control, audit trail and accuracy suite then apply without further code.
Tests: `core/tests/test_ai_harness.py` (backend) and `src/app/shared/ask-button.component.spec.ts` (frontend).
