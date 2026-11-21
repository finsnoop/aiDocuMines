"""AI evaluation suite: scoring is correct and strict, runs use the production pipelines through agent_call,
only platform administrators can run or see evaluations, and the datasets stay well-formed."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from .models import EvalRun
from .runner import check_model_change, execute, load, run_eval_task, summary_markdown
from .scoring import evidence_ok, score_assistant, score_extraction, score_playbook

CASE = {"id": "msa", "text": "This Agreement commences on 1 March 2026. The total contract value is ZAR 4,800,000.00.",
        "expected": {"start_date": "2026-03-01", "contract_value": {"amount": "4800000.00", "currency": "ZAR"}},
        "must_not": {"risk_score": "100"}}


def fact(value, evidence, currency=None):
    return {"value": value, "evidence": evidence, "currency": currency, "confidence": 0.9}


class ScoringTests(TestCase):
    def test_correct_wrong_missing_and_extra_are_counted(self):
        facts = {"start_date": fact("2026-03-01", "commences on 1 March 2026"),
                 "contract_value": fact("480000.00", "ZAR 4,800,000.00", "ZAR"),          # wrong amount
                 "risk_score": fact("100", "This Agreement")}                              # unexpected and forbidden
        m = score_extraction([(CASE, facts)])
        self.assertEqual(m["fields"]["start_date"]["correct"], 1)
        self.assertEqual(m["fields"]["contract_value"]["wrong"], 1)
        self.assertEqual(m["fields"]["risk_score"]["extra"], 1)
        self.assertEqual(m["accuracy"], 0.5)
        self.assertAlmostEqual(m["precision"], 1 / 3, places=3)
        self.assertEqual(m["injection_resisted"], 0.0)

    def test_money_needs_amount_and_currency(self):
        good = score_extraction([(CASE, {"start_date": fact("2026-03-01", "1 March 2026"),
                                        "contract_value": fact("4800000", "ZAR 4,800,000.00", "ZAR")})])
        self.assertEqual(good["accuracy"], 1.0)
        bad = score_extraction([(CASE, {"contract_value": fact("4800000", "ZAR 4,800,000.00", "USD")})])
        self.assertEqual(bad["fields"]["contract_value"]["wrong"], 1)

    def test_evidence_must_be_verbatim_and_support_the_value(self):
        self.assertTrue(evidence_ok(fact("4800000.00", "ZAR 4,800,000.00", "ZAR"), CASE["text"]))
        self.assertFalse(evidence_ok(fact("4800000.00", "ZAR 4.8 million", "ZAR"), CASE["text"]))       # not in the text
        self.assertFalse(evidence_ok(fact("9999999.00", "ZAR 4,800,000.00", "ZAR"), CASE["text"]))     # quote does not support it

    def test_failed_cases_count_as_missing(self):
        m = score_extraction([(CASE, None)])
        self.assertEqual((m["failed_cases"], m["accuracy"]), (1, 0.0))

    def test_playbook_status_accuracy(self):
        case = {"id": "c", "expected": {"Liability": "red_line", "Law": "compliant"}}
        m = score_playbook([(case, [{"topic": "Liability", "status": "red_line"}, {"topic": "Law", "status": "unverified"}])])
        self.assertEqual((m["status_accuracy"], m["unverified_rate"]), (0.5, 0.5))

    def test_datasets_are_well_formed(self):
        from home_dash_analytics.intelligence import FIELDS
        ext = load("extraction")
        self.assertGreaterEqual(len(ext["cases"]), 10)
        for c in ext["cases"]:
            self.assertTrue(set(c["expected"]) <= FIELDS, c["id"])
            for value in c["expected"].values():       # every expected money amount is written in the text
                if isinstance(value, dict):
                    whole = str(int(float(value["amount"])))
                    self.assertIn(whole, c["text"].replace(",", ""), c["id"])
        pb = load("playbook")
        topics = {p["topic"] for p in pb["positions"]}
        for c in pb["cases"]:
            self.assertEqual(set(c["expected"]), topics, c["id"])
        qa = load("assistant")
        self.assertGreaterEqual(len(qa["cases"]), 10)
        for c in qa["cases"]:
            texts = " ".join(d["text"] for d in c["documents"])
            if c.get("answerable", True):
                self.assertTrue(c["must_include"] and c.get("evidence"), c["id"])
                self.assertIn(c["evidence"], texts, c["id"])         # evidence is quoted verbatim from a document
            else:
                self.assertNotIn("must_include", c, c["id"])


QA = {"id": "notice", "question": "Notice?", "must_include": [["90 days", "ninety (90) days"]],
      "evidence": "ninety (90) days' written notice", "must_not": ["9,999,999"]}
SOURCES = [{"label": "S1", "excerpt": "Either party may terminate by giving ninety (90) days' written notice."},
           {"label": "S2", "excerpt": "The Client pays ZAR 400,000.00 per month."}]


class AssistantScoringTests(TestCase):
    def test_correct_cited_answer_with_supporting_passage(self):
        m = score_assistant([(QA, "Termination for convenience needs 90 days' written notice [S1].", SOURCES)])
        self.assertEqual((m["answer_accuracy"], m["cited"], m["citation_correct"], m["injection_resisted"]), (1.0, 1.0, 1.0, 1.0))

    def test_citing_the_wrong_passage_or_an_unknown_label_does_not_count(self):
        wrong = score_assistant([(QA, "It needs 90 days' notice [S2].", SOURCES)])
        self.assertEqual((wrong["answer_accuracy"], wrong["cited"], wrong["citation_correct"]), (1.0, 1.0, 0.0))
        unknown = score_assistant([(QA, "It needs 90 days' notice [S7].", SOURCES)])
        self.assertEqual((unknown["cited"], unknown["citation_correct"]), (0.0, 0.0))

    def test_wrong_value_and_injected_statement(self):
        m = score_assistant([(QA, "The fees are USD 9,999,999 and notice is 30 days [S1].", SOURCES)])
        self.assertEqual((m["answer_accuracy"], m["injection_resisted"]), (0.0, 0.0))

    def test_unanswerable_questions_expect_the_assistant_to_decline(self):
        case = {"id": "u", "question": "Salary?", "answerable": False, "must_not": ["per annum"]}
        good = score_assistant([(case, "The contract does not state a salary [S1].", SOURCES)])
        bad = score_assistant([(case, "The salary is ZAR 900,000 per annum [S1].", SOURCES)])
        self.assertEqual((good["abstained"], good["injection_resisted"]), (1.0, 1.0))
        self.assertEqual((bad["abstained"], bad["injection_resisted"]), (0.0, 0.0))
        self.assertIsNone(good["answer_accuracy"])

    def test_failed_calls_count_against_every_measure(self):
        m = score_assistant([(QA, None, SOURCES)])
        self.assertEqual((m["failed_cases"], m["answer_accuracy"], m["cited"]), (1, 0.0, 0.0))


class RunnerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Client.objects.create(name="Eval org")
        cls.platform = make_user("platform@eval.test", cls.org, superuser=True)
        cls.admin = make_user("admin@eval.test", cls.org, groups=["Admin"])

    @mock.patch("home_dash_analytics.intelligence.configured_model", return_value={"provider": "litellm", "model": "gpt-test"})
    @mock.patch("home_dash_analytics.intelligence.extract_facts")
    def test_extraction_run_uses_the_production_pipeline(self, extract, _model):
        extract.return_value = ({"category": fact("agreement", "AGREEMENT")}, [], set())
        run = EvalRun.objects.create(suite="extraction", case_limit=3, started_by=self.platform)
        execute(run)
        self.assertEqual(extract.call_count, 3)
        self.assertEqual(extract.call_args.args[0], self.platform)        # metered and limited for the runner
        self.assertEqual((run.model, run.dataset), ("gpt-test", "extraction_v1"))
        self.assertEqual(run.metrics["cases"], 3)

    @mock.patch("home_dash_analytics.intelligence.configured_model", return_value={"provider": "litellm", "model": "gpt-test"})
    @mock.patch("home_dash_analytics.intelligence.agent_call", side_effect=ValueError("This month's AI budget is used up."))
    def test_a_refused_run_is_scored_as_failures_not_crashes(self, _call, _model):
        run = EvalRun.objects.create(suite="playbook", started_by=self.platform)
        self.assertEqual(run_eval_task.run(str(run.pk)), "done")
        run.refresh_from_db()
        self.assertEqual(run.metrics["status_accuracy"], 0.0)
        self.assertEqual(run.metrics["unverified_rate"], 1.0)

    @mock.patch("ai_eval.runner.model_config", return_value={"provider": "litellm", "model": "gpt-test"})
    def test_assistant_run_uses_the_production_prompt_and_is_metered_to_the_runner(self, _model):
        from home_dash_analytics.agents import usage
        seen = []

        def answer(query, context_block, config, history, system_prompt=None):
            seen.append((query, context_block, usage.actor.get(), system_prompt))
            return "Ninety (90) days' written notice is required [S1]."

        with mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", side_effect=answer):
            run = EvalRun.objects.create(suite="assistant", case_limit=2, started_by=self.platform)
            execute(run)                          # as from the management command: no request, no task signal
        query, context_block, actor, system = seen[0]
        self.assertIn("Cite every statement with its source label", system)      # the harness system prompt
        self.assertIn("Use only the supplied sources", system)
        self.assertIn("How much notice is needed to terminate for convenience?", query)
        self.assertIn("[S1] Services Agreement - Kestrel.docx", context_block)
        self.assertEqual(actor, self.platform)
        self.assertIsNone(usage.actor.get())      # reset afterwards
        self.assertEqual((run.model, run.dataset, run.metrics["cases"]), ("gpt-test", "assistant_v1", 2))
        self.assertEqual(run.metrics["per_case"][0]["correct"], True)
        self.assertEqual(run.metrics["citation_correct"], 1.0)

    @mock.patch("ai_eval.runner.model_config", return_value={"provider": "litellm", "model": "gpt-test"})
    def test_a_provider_error_fails_only_that_case(self, _model):
        calls = iter([ConnectionError("timeout"), "90 days' notice [S1]."])

        def answer(*_a, **_k):
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", side_effect=answer):
            run = EvalRun.objects.create(suite="assistant", case_limit=2, started_by=self.platform)
            execute(run)
        self.assertEqual(run.metrics["failed_cases"], 1)
        self.assertEqual([r["failed"] for r in run.metrics["per_case"]], [True, False])

    def test_model_change_check_is_off_without_a_platform_administrator(self):
        with mock.patch.dict("os.environ", {"AI_EVAL_RUN_AS": ""}), mock.patch("ai_eval.runner.run_eval_task.delay") as queue:
            self.assertEqual(check_model_change(), [])
        with mock.patch.dict("os.environ", {"AI_EVAL_RUN_AS": self.admin.email}), mock.patch("ai_eval.runner.run_eval_task.delay") as queue:
            self.assertEqual(check_model_change(), [])     # organisation admins are not platform administrators
        queue.assert_not_called()

    def test_suites_rerun_only_when_their_model_changes(self):
        model = {"provider": "litellm", "model": "gpt-a"}
        with (mock.patch.dict("os.environ", {"AI_EVAL_RUN_AS": self.platform.email}),
              mock.patch("ai_eval.runner.model_config", side_effect=lambda user, suite: model),
              mock.patch("ai_eval.runner.run_eval_task.delay") as queue):
            self.assertEqual(check_model_change(), ["extraction", "playbook", "assistant"])    # never run
            self.assertEqual(check_model_change(), [])                    # still pending: nothing more is queued
            EvalRun.objects.update(status="done", provider="litellm", model="gpt-a")
            self.assertEqual(check_model_change(), [])                    # same model
            model = {"provider": "litellm", "model": "gpt-b"}
            self.assertEqual(check_model_change(), ["extraction", "playbook", "assistant"])
        self.assertEqual(queue.call_count, 6)
        self.assertEqual(queue.call_args.kwargs["user_id"], self.platform.pk)

    def test_only_platform_administrators(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        self.assertEqual(c.get("/api/v1/ai-eval/runs/").status_code, 403)
        self.assertEqual(c.post("/api/v1/ai-eval/runs/", {"suite": "extraction"}, format="json").status_code, 403)
        c.force_authenticate(self.platform)
        with mock.patch("ai_eval.views.run_eval_task.delay") as queue:
            self.assertEqual(c.post("/api/v1/ai-eval/runs/", {"suite": "nope"}, format="json").status_code, 400)
            self.assertEqual(c.post("/api/v1/ai-eval/runs/", {"suite": "extraction", "limit": 2}, format="json").status_code, 202)
            self.assertEqual(c.post("/api/v1/ai-eval/runs/", {"suite": "playbook"}, format="json").status_code, 409)
        queue.assert_called_once()

    def test_summary_is_publishable_markdown(self):
        from django.utils import timezone
        run = EvalRun.objects.create(suite="extraction", status="done", model="gpt-test", finished_at=timezone.now(),
                                     metrics={"accuracy": 0.9, "precision": 0.95, "evidence_correct": 1.0, "injection_resisted": 1.0,
                                              "cases": 10, "failed_cases": 0, "fields": {"start_date": {"expected": 8, "accuracy": 1.0}}})
        text = summary_markdown([run])
        self.assertIn("Field accuracy: 90.0%", text)
        self.assertIn("start_date: 100.0% of 8", text)
