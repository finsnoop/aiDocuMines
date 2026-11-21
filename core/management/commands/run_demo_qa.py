"""
Run the corpus QA benchmark (qa_benchmark.json) against a loaded demo organisation.

Checks, per question, as a named person of the demo organisation (never as a platform superuser):
  documents   every expected document is stored in the right matter
  text        every expected document has extracted text (OCR for scans); a password-protected PDF must NOT
              have text until someone unlocks it
  indexed     every expected document has search chunks (semantic search and the Assistant can find it)
  scope       the matter context the Assistant uses holds the expected documents and nothing from other matters
  task        task-specific checks: OCR facts present in the scan's text; privileged and isolated documents
              refused to people who should not see them
  answer      (--ask only) the Assistant's answer on the matter covers the expected key facts (approximate:
              numbers and key words of each fact must appear; read the answers in the report)

  python manage.py run_demo_qa --corpus /path/Legal_Test_Repository
  python manage.py run_demo_qa --corpus ... --ask --report /tmp/qa.md     # also asks the Assistant (uses AI credit)
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

STOP = set("""the and for with from that this than into over under were was are has have had its their they them
which what when where who whom whose not but all any each more most other some such only same also very just
versus between within without about after before during while upon onto per via""".split())

PRIVILEGED_PROBES = (  # (local part, why they must not see a privileged document)
    ("aisha.associate", "a general matter member without privilege clearance"),
    ("client.guest", "a guest who is not on the matter"),
)


def key_tokens(fact: str) -> list:
    """Numbers (with their units) and significant words of an expected fact."""
    text = fact.lower().replace(",", "")
    numbers = re.findall(r"\d+(?:\.\d+)?%?x?", text)
    words = [w for w in re.findall(r"[a-z][a-z\-']{3,}", text) if w not in STOP]
    return list(dict.fromkeys(numbers + words))


def fact_covered(fact: str, answer: str) -> bool:
    tokens = key_tokens(fact)
    if not tokens:
        return True
    text = (answer or "").lower().replace(",", "")
    hits = sum(1 for t in tokens if t in text)
    numbers = [t for t in tokens if t[0].isdigit()]
    return hits / len(tokens) >= 0.6 and all(n in text for n in numbers)


class Command(BaseCommand):
    help = "Run the corpus QA benchmark against the loaded demo organisation and report pass/fail."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", required=True, help="Folder with qa_benchmark.json and index.json.")
        parser.add_argument("--org", default="Datasqan Demo Law Firm")
        parser.add_argument("--domain", default="demo.datasqan.com")
        parser.add_argument("--as", dest="actor", default="", help="Person the checks run as (default james.partner@DOMAIN).")
        parser.add_argument("--task", action="append", default=[], help="Only these tasks (repeatable).")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--ask", action="store_true", help="Also ask the Assistant each question (uses AI credit).")
        parser.add_argument("--report", default="", help="Write a Markdown report here.")

    def handle(self, *args, corpus, org, domain, actor, task, limit, ask, report, **opts):
        from core.access import is_platform_superuser
        from custom_authentication.models import Client, CustomUser
        qa_path, index_path = os.path.join(corpus, "qa_benchmark.json"), os.path.join(corpus, "index.json")
        if not (os.path.isfile(qa_path) and os.path.isfile(index_path)):
            raise CommandError("The corpus folder must contain qa_benchmark.json and index.json.")
        questions = json.load(open(qa_path, encoding="utf-8"))
        index = {r["doc_id"]: r for r in json.load(open(index_path, encoding="utf-8"))}
        if task:
            questions = [q for q in questions if q["task"] in task]
        if limit:
            questions = questions[:limit]
        firm = Client.objects.filter(name=org).first()
        if firm is None:
            raise CommandError(f"Organisation {org!r} not found (load it with load_demo_corpus).")
        actor = actor or f"james.partner@{domain}"
        self.people = {u.email.split("@")[0].lower(): u for u in
                       CustomUser.objects.filter(email__iendswith="@" + domain, is_active=True)}
        self.isolated = CustomUser.objects.filter(email__iexact=f"tenant.admin@isolation.{domain}").first()
        person = CustomUser.objects.filter(email__iexact=actor, client=firm, is_active=True).first()
        if person is None:
            raise CommandError(f"{actor} is not an active member of {org!r}.")
        if is_platform_superuser(person):
            raise CommandError("Run the QA as a member of the demo organisation, not a platform superuser.")
        self.firm, self.person, self.index, self.ask = firm, person, index, ask
        self.config = None
        if ask:
            from ai_eval.runner import model_config
            self.config = model_config(person, "assistant")

        rows = [self.check(q) for q in questions]
        self.print_summary(rows)
        if report:
            with open(report, "w", encoding="utf-8") as fh:
                fh.write(self.markdown(rows))
            self.stdout.write(f"Report written to {report}")

    # ── lookups ─────────────────────────────────────────────────────────────────────────────
    def matter(self, matter_id):
        from core.models import Matter
        return Matter.objects.filter(client=self.firm, reference=matter_id).first()

    def matter_files(self, matter):
        from core.models import File
        files = {}
        for f in File.objects.filter(user__client=self.firm, project_id=matter.name, service_id="matter"):
            m = re.match(r"([A-Z]{2,4}-\d{3}-\d{3})", f.filename or "")
            if m and m.group(1) not in files:
                files[m.group(1)] = f
        return files

    # ── one question ────────────────────────────────────────────────────────────────────────
    def check(self, q):
        from core.assistant import file_text
        from document_search.models import VectorChunk
        row = {"id": q["test_id"], "task": q["task"], "question": q["question"], "checks": {}, "notes": []}
        matter = self.matter(q["matter_id"])
        if matter is None:
            row["checks"]["documents"] = False
            row["notes"].append(f"matter {q['matter_id']} not found")
            return self.finish(row)
        files = self.matter_files(matter)
        expected = q["expected_document_ids"]
        missing = [d for d in expected if d not in files]
        row["checks"]["documents"] = not missing
        if missing:
            row["notes"].append(f"missing documents: {', '.join(missing)}")
        present = [files[d] for d in expected if d in files]

        encrypted = q["task"] == "encrypted_pdf"
        texts = {f.pk: file_text(f) for f in present}
        if encrypted:
            leaked = [f.filename for f in present if texts[f.pk].strip()]
            row["checks"]["text"] = not leaked
            if leaked:
                row["notes"].append("password-protected PDF has text without an authorised unlock")
        else:
            empty = [f.filename for f in present if not texts[f.pk].strip()]
            row["checks"]["text"] = not empty
            if empty:
                row["notes"].append(f"no text: {', '.join(empty)}")
            unindexed = [f.filename for f in present
                         if not VectorChunk.objects.filter(file=f).exists() and texts[f.pk].strip()]
            row["checks"]["indexed"] = not unindexed
            if unindexed:
                row["notes"].append(f"not indexed for search: {', '.join(unindexed[:5])}"
                                    f"{' …' if len(unindexed) > 5 else ''}")

        if q["task"] == "matter_search":
            self.check_scope(row, matter, files)
        elif q["task"] == "ocr":
            combined = " ".join(texts.values()).lower().replace(",", "")
            lost = [f for f in q["expected_key_facts"] if f.lower().replace(",", "") not in combined]
            row["checks"]["task"] = not lost
            if lost:
                row["notes"].append(f"OCR text lacks: {'; '.join(lost)}")
        elif q["task"] == "rbac_privilege":
            self.check_privilege(row, present)

        if self.ask and not encrypted:
            self.check_answer(row, q, matter)
        return self.finish(row)

    def check_scope(self, row, matter, files):
        from core.assistant import resolve_context
        try:
            ctx = resolve_context(self.person, {"type": "matter", "id": str(matter.pk)})
        except Exception as exc:
            row["checks"]["scope"] = False
            row["notes"].append(f"matter context failed: {exc}")
            return
        in_scope = {f.pk for f in ctx.files}
        outside = [f.filename for f in ctx.files if not (f.filename or "").startswith(matter.reference)]
        absent = [d for d, f in files.items() if f.pk not in in_scope]
        row["checks"]["scope"] = not outside and not absent
        if outside:
            row["notes"].append(f"documents from other matters in scope: {', '.join(outside[:5])}")
        if absent:
            row["notes"].append(f"matter documents missing from the Assistant's scope: {', '.join(absent[:5])}")

    def check_privilege(self, row, present):
        from core.access import can_access_file
        privileged = [f for f in present
                      if self.index.get(f.filename[:10], {}).get("classification") == "LEGAL_PRIVILEGED"]
        if not privileged:
            row["checks"]["task"] = False
            row["notes"].append("no privileged document found")
            return
        ok = True
        probes = [(self.people.get(local), why) for local, why in PRIVILEGED_PROBES]
        probes.append((self.isolated, "an administrator of another organisation"))
        for user, why in probes:
            if user is None:
                continue
            for f in privileged:
                if can_access_file(user, f):
                    ok = False
                    row["notes"].append(f"{user.email} ({why}) can open {f.filename}")
        row["checks"]["task"] = ok

    def check_answer(self, row, q, matter):
        from core import ai_harness as harness
        from core.assistant import build_sources, resolve_context
        from grid_documents_interrogation.utils import dispatch_to_llm
        try:
            ctx = resolve_context(self.person, {"type": "matter", "id": str(matter.pk)})
            sources = build_sources(q["question"], ctx, self.person)
            answer = harness.run(ctx, q["question"], sources, [], [], harness.settings_for(self.person),
                                 lambda message, evidence, turns, system: dispatch_to_llm(
                                     message, evidence, self.config, turns, system_prompt=system))["answer"]
        except Exception as exc:
            row["checks"]["answer"] = False
            row["notes"].append(f"Assistant failed: {exc}")
            return
        answer = answer if isinstance(answer, str) else json.dumps(answer, default=str)
        facts = q["expected_key_facts"]
        covered = [f for f in facts if fact_covered(f, answer)]
        row["answer"] = answer
        row["facts"] = f"{len(covered)}/{len(facts)}"
        row["checks"]["answer"] = len(covered) * 2 >= len(facts)
        missed = [f for f in facts if f not in covered]
        if missed:
            row["notes"].append(f"answer may miss: {'; '.join(missed[:3])}")

    @staticmethod
    def finish(row):
        row["passed"] = all(row["checks"].values())
        return row

    # ── output ──────────────────────────────────────────────────────────────────────────────
    def print_summary(self, rows):
        by_task = defaultdict(lambda: [0, 0])
        for r in rows:
            by_task[r["task"]][0] += r["passed"]
            by_task[r["task"]][1] += 1
        failed_checks = Counter(name for r in rows for name, ok in r["checks"].items() if not ok)
        passed = sum(r["passed"] for r in rows)
        self.stdout.write(f"\nQA benchmark as {self.person.email}: {passed}/{len(rows)} questions passed\n")
        for name, (ok, total) in sorted(by_task.items()):
            self.stdout.write(f"  {name:<28} {ok}/{total}")
        if failed_checks:
            self.stdout.write("\nFailed checks: " + ", ".join(f"{k} ×{v}" for k, v in failed_checks.most_common()))
        for r in rows:
            if not r["passed"]:
                self.stdout.write(self.style.ERROR(f"  FAIL {r['id']} ({r['task']}): " + " | ".join(r["notes"])))

    def markdown(self, rows):
        passed = sum(r["passed"] for r in rows)
        out = [f"# QA benchmark: {self.firm.name}", "",
               f"Run as {self.person.email}. {passed}/{len(rows)} questions passed."
               f"{' The Assistant was asked every question.' if self.ask else ' Assistant answers not requested (--ask).'}",
               "", "| Question | Task | Result | Checks | Notes |", "|---|---|---|---|---|"]
        for r in rows:
            checks = ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in r["checks"].items())
            facts = f" (facts {r['facts']})" if "facts" in r else ""
            notes = "; ".join(r["notes"]).replace("|", "\\|")
            out.append(f"| {r['id']} | {r['task']} | {'PASS' if r['passed'] else 'FAIL'}{facts} | {checks} | {notes} |")
        answered = [r for r in rows if r.get("answer")]
        if answered:
            out += ["", "## Assistant answers", ""]
            for r in answered:
                out += [f"### {r['id']}: {r['question']}", "", r["answer"].strip(), ""]
        return "\n".join(out) + "\n"
