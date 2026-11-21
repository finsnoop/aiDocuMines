"""
Run the AI evaluation suite against the configured models, using the production code paths:
document intelligence (home_dash_analytics.intelligence.extract_facts), playbook review
(contracts.playbook.build_messages / validate) and Ask Assistant (core.assistant passage ranking and prompt,
grid_documents_interrogation.utils.dispatch_to_llm). Every model call goes through the AI gateway, so limits and
metering apply to the person running the evaluation.

Model changes: a daily task (check_model_change) queues a suite again when the model it would use differs from
the model of its last run. It runs as the platform administrator named in AI_EVAL_RUN_AS.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

from celery import shared_task
from django.utils import timezone

from .scoring import score_assistant, score_extraction, score_playbook

logger = logging.getLogger(__name__)
DATASETS = Path(__file__).resolve().parent / "datasets"
SUITES = {"extraction": "extraction_v1.json", "playbook": "playbook_v1.json", "assistant": "assistant_v1.json"}


def load(suite):
    return json.loads((DATASETS / SUITES[suite]).read_text(encoding="utf-8"))


def _case_failed(case, exc):
    # A refused or failed call (budget used up, plan rejected, provider timeout) is scored as a failed case,
    # so one bad call does not discard the rest of the run.
    logger.info("Evaluation case %s failed: %s: %s", case["id"], type(exc).__name__, exc)


def run_extraction(user, config, dataset, limit=None):
    from home_dash_analytics.intelligence import extract_facts
    results = []
    for case in dataset["cases"][:limit]:
        try:
            facts, _, _ = extract_facts(user, config, case["text"], [], case["filename"])
        except Exception as exc:
            _case_failed(case, exc)
            facts = None
        results.append((case, facts))
    return score_extraction(results)


def run_playbook(user, config, dataset, limit=None):
    from contracts.playbook import build_messages, validate
    from home_dash_analytics.intelligence import agent_call
    positions = [SimpleNamespace(pk=i + 1, topic=p["topic"], standard=p["standard"], fallbacks=p.get("fallbacks", []),
                                 red_lines=p.get("red_lines", []), guidance=p.get("guidance", ""), severity=p.get("severity", "medium"))
                 for i, p in enumerate(dataset["positions"])]
    results = []
    for case in dataset["cases"][:limit]:
        messages, _ = build_messages(positions, case["text"])
        try:
            raw = agent_call(user, config, messages, "ai_eval.playbook")
            findings = validate(raw, positions, case["text"])
        except Exception as exc:
            _case_failed(case, exc)
            findings = None
        results.append((case, findings))
    return score_playbook(results)


def run_assistant(user, config, dataset, limit=None):
    from core import ai_harness as harness
    from core.assistant import Context, build_sources
    from grid_documents_interrogation.utils import dispatch_to_llm
    settings = {"surfaces": {}, "guardrails": dict(harness.GUARDRAIL_DEFAULTS)}   # the platform defaults
    results = []
    for case in dataset["cases"][:limit]:
        files = [SimpleNamespace(id=-(i + 1), pk=-(i + 1), filename=d["filename"], content=d["text"])
                 for i, d in enumerate(case["documents"])]
        kind, label = ("file", files[0].filename) if len(files) == 1 else ("folder", "Evaluation folder")
        ctx = Context(kind=kind, label=label, files=files, total_in_scope=len(files))
        sources = build_sources(case["question"], ctx, user, semantic=False)
        try:
            answer = harness.run(ctx, case["question"], sources, [], [], settings,
                                 lambda message, evidence, turns, system: dispatch_to_llm(
                                     message, evidence, config, turns, system_prompt=system))["answer"]
        except Exception as exc:
            _case_failed(case, exc)
            answer = None
        results.append((case, answer, sources))
    return score_assistant(results)


def model_config(user, suite):
    """The cleaned model settings the suite's production feature would use for this person."""
    if suite == "assistant":
        from core.assistant import assistant_model
        from grid_documents_interrogation.safety import clean_llm_config
        provider, model = assistant_model(user)
        return clean_llm_config({"provider": provider, "model": model}, user)
    from home_dash_analytics.intelligence import configured_model
    return configured_model(user)


def execute(run):
    from home_dash_analytics.agents.usage import acting_as
    user = run.started_by
    with acting_as(user):          # metered and limited as the person running it (also from the command line)
        config = model_config(user, run.suite)
        run.provider, run.model = str(config.get("provider", "")), str(config.get("model", ""))
        dataset = load(run.suite)
        run.dataset = dataset["name"]
        runner = {"extraction": run_extraction, "playbook": run_playbook, "assistant": run_assistant}[run.suite]
        run.metrics = runner(user, config, dataset, run.case_limit)
    return run


@shared_task(name="ai_eval.run", soft_time_limit=3000, time_limit=3300)
def run_eval_task(run_id, user_id=None):
    from .models import EvalRun
    run = EvalRun.objects.select_related("started_by").get(pk=run_id)
    run.status = "running"
    run.save(update_fields=["status"])
    try:
        execute(run)
        run.status = "done"
    except Exception as exc:
        logger.warning("AI evaluation %s failed", run_id, exc_info=True)
        run.status, run.error = "failed", str(exc)[:1000]
    run.finished_at = timezone.now()
    run.save()
    return run.status


def evaluation_user():
    """The platform administrator scheduled evaluations run as (AI_EVAL_RUN_AS), or None."""
    from django.contrib.auth import get_user_model
    from core.access import is_platform_superuser
    email = os.getenv("AI_EVAL_RUN_AS", "").strip()
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first() if email else None
    return user if user is not None and is_platform_superuser(user) else None


@shared_task(name="ai_eval.check_model_change")
def check_model_change():
    """Queue each suite whose model differs from its last finished run (or that has never run)."""
    from .models import EvalRun
    user = evaluation_user()
    if user is None:
        logger.info("AI evaluation on model change is off: set AI_EVAL_RUN_AS to an active platform administrator.")
        return []
    if EvalRun.objects.filter(status__in=["pending", "running"]).exists():
        return []
    queued = []
    for suite in SUITES:
        config = model_config(user, suite)
        current = (str(config.get("provider", "")), str(config.get("model", "")))
        last = EvalRun.objects.filter(suite=suite, status__in=["done", "failed"]).first()
        if last is not None and (last.provider, last.model) == current:
            continue
        run = EvalRun.objects.create(suite=suite, started_by=user)
        queued.append(suite)
        run_eval_task.delay(str(run.pk), user_id=user.pk)
    if queued:
        logger.info("AI evaluation queued after a model change: %s", ", ".join(queued))
    return queued


def summary_markdown(runs):
    """A publishable summary of the latest results per suite and model."""
    lines = ["# aiDocuMines AI accuracy", "",
             "Measured on synthetic, labelled documents with the production pipeline. A fact only counts when the",
             "model quotes evidence that appears verbatim in the document.", ""]
    for r in runs:
        m = r.metrics or {}
        lines.append(f"## {r.get_suite_display()}: {r.model or 'configured model'} ({r.finished_at:%Y-%m-%d})")
        if r.suite == "extraction":
            lines += [f"- Field accuracy: {_pct(m.get('accuracy'))}", f"- Precision (no unsupported claims): {_pct(m.get('precision'))}",
                      f"- Evidence verbatim and supporting the value: {_pct(m.get('evidence_correct'))}",
                      f"- Prompt-injection resisted: {_pct(m.get('injection_resisted'))}",
                      f"- Documents: {m.get('cases')} ({m.get('failed_cases', 0)} not analysed)"]
            for field, f in (m.get("fields") or {}).items():
                if f.get("expected"):
                    lines.append(f"  - {field}: {_pct(f.get('accuracy'))} of {f['expected']}")
        elif r.suite == "assistant":
            lines += [f"- Correct answers: {_pct(m.get('answer_accuracy'))}",
                      f"- Answers citing their sources: {_pct(m.get('cited'))}",
                      f"- Cited passage contains the supporting evidence: {_pct(m.get('citation_correct'))}",
                      f"- Says so when the documents do not answer: {_pct(m.get('abstained'))}",
                      f"- Prompt-injection resisted: {_pct(m.get('injection_resisted'))}",
                      f"- Questions: {m.get('cases')} ({m.get('failed_cases', 0)} not answered)"]
        else:
            lines += [f"- Playbook status accuracy: {_pct(m.get('status_accuracy'))}",
                      f"- Findings that could not be verified: {_pct(m.get('unverified_rate'))}",
                      f"- Contracts: {m.get('cases')}"]
        lines.append("")
    return "\n".join(lines)


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"
