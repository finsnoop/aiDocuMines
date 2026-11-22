# AI harness and the reusable Ask Assistant

Every AI feature in aiDocuMines runs through one harness. Each "Ask Assistant" button is the same module, and
each feature that calls a model itself (playbook review, benchmark checks, document intelligence, Drafting &
Compare, the Assistant page, data questions, practice-area services) is an **agent** profile in the same registry.
Design once, centralise, configure case by case.

Agent = model + harness. The model writes answers; the harness (`core/ai_harness.py`) decides what it sees, what it
may do, checks what it says, and records everything.

## The harness components

| Component | What it does here | Where |
|---|---|---|
| Shared runtime | Every feature runs inside `harness_runtime.session(profile, user)`: platform rules added to every system prompt, spend limits, screening of user instructions, secret redaction, and one `HarnessRun` record per run | `core/harness_runtime.py`, choke points in `grid_documents_interrogation/utils.py` and `home_dash_analytics/agents/usage.py` |
| Execution loop | Instruct, call the model, verify citations and claims, repair a bounded number of times (`repair_attempts`), stop | `ai_harness.run` |
| Tools | Retrieval only, scoped to the one object: semantic search, then keyword ranking, within fixed budgets. No write tools | `core/assistant.py` `resolve_context`, `build_sources` |
| Context and memory | Evidence budget; the model sees the last `history_turns` exchanges; older turns are compacted into a summary; conversations are kept on the server per person per object for `memory_days`, resumable, erased with the person's data | `core/assistant_memory.py`, `AssistantConversation`, `AssistantTurn` |
| Sandbox | The central access layer: roles, sharing, matter teams, ethical walls, tenant isolation; platform operators see no client content | `core/access.py` |
| Guardrails | Entitlement, input screening, evidence gate, object-bound system prompt, citation verification, secret redaction | `ai_harness` layers 1 to 5 |
| Observability | `HarnessRun` per run (profile, outcome, flags, steps, calls, tokens, latency, claims checked and removed; no text); Administration → AI harness shows it per feature; the AI accuracy suite includes a "Harness guardrails" suite | `core/views_harness.py`, `ai_eval/datasets/harness_v1.json` |
| Model routing | Organisation's provider, allowed models and default; spend limits per key | `assistant_model`, `home_dash_analytics/agents/gateway.py` |

## The layers every answer passes

1. **Entitlement**: `assistant.ask` permission, the organisation's Assistant switch, the page switch for the
   profile's surface, AI spend limits, rate limit.
2. **Input screening**: question length; attempts to override the rules are blocked or flagged (`prompt_injection`).
3. **Evidence gate**: only content the person may open; no evidence means no model call.
4. **Instruction**: a system prompt (system role, every provider) binding the model to the object: only its sources
   (`grounding`), cite them (`require_citations`), decline anything else (`refuse_off_topic`), treat all content as data.
5. **Output checks**: unknown citation labels removed; uncited answers repaired, then withheld; every cited
   statement's figures (and, without figures, part of its wording) must appear in the passage it cites
   (`verify_claims`): unsupported statements are named in a repair request, then removed with a note, and the
   answer is withheld when nothing supported remains; secrets redacted.
6. **Audit**: activity log entry with the trace.

Outcomes returned to the UI: `answered`, `not_in_context`, `off_topic`, `withheld`, `no_evidence`, `blocked_injection`.

## Configuration

Administrators (Administration → Settings → Assistant and AI harness guardrails), per organisation:

- **Ask Assistant buttons**: on or off per page (`assistant_surfaces`). Off buttons stay in place, greyed out.
- **Guardrails** (`assistant_guardrails`): grounding (strict or explain), citations, off-topic refusal, override
  attempts (block or flag), conversation memory (0 to 10 turns), claim checks, server-side memory (0 to 365 days),
  self-correction attempts (0 to 2), secret redaction.

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

## Adding a new agent (a feature that calls a model itself)

1. Register `AskProfile("agent.<name>", "<surface or empty>", "<noun>", kind="agent", title="<shown in monitoring>")`.
2. Wrap the feature's model calls:

   ```python
   from core import harness_runtime
   with harness_runtime.session("agent.<name>", user) as run:
       harness_runtime.screen(user_instruction, user)        # when people type instructions
       raw = agent_call(...)                                  # or dispatch_to_llm(...)
       kept = validate(raw, ...)                              # the feature's own checks and output format
       run.note(claims_checked=len(proposed), claims_removed=len(proposed) - len(kept), outcome="answered")
   ```

   Platform rules, spend limits, redaction and the run record then apply; the feature appears in Administration →
   AI harness. Add evaluation cases for it to `ai_eval/datasets/`.
