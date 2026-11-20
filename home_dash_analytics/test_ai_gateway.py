"""AI gateway: spend by key and person for any dates, limits that switch on and off, and enforcement."""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from cost_centre.models import EventLog, EventType, ServiceType
from custom_authentication.models import Client
from .agents import gateway, usage
from .models import AISpendLimit, ModelPrice


class AIGatewayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Client.objects.create(name="Gateway firm")
        cls.other = Client.objects.create(name="Other firm")
        cls.ceo = make_user("ceo@gateway.test", cls.org, groups=["Executive"])
        cls.manager = make_user("manager@gateway.test", cls.org, groups=["Manager"])
        cls.analyst = make_user("analyst@gateway.test", cls.org, groups=["Client"])
        cls.outsider = make_user("outsider@other.test", cls.other, groups=["Admin"])
        ModelPrice.objects.create(provider="*", model="gpt-test", input_usd_per_million=Decimal("10"),
                                  output_usd_per_million=Decimal("20"))

    def setUp(self):
        cache.clear()

    def spend(self, user, *, tokens=1000, prompt=600, completion=400, key="platform", model="gpt-test", days_ago=0):
        event = EventLog.objects.create(user=user, tenant_id=user.client_id, event_type=EventType.OTHER,
                                        service_type=ServiceType.PAYABLE, tokens_used=tokens,
                                        metadata={"provider": "litellm", "model": model, "task": "chat", "key": key,
                                                  "prompt_tokens": prompt, "completion_tokens": completion})
        if days_ago:
            EventLog.objects.filter(pk=event.pk).update(created_at=timezone.now() - dt.timedelta(days=days_ago))
        gateway.ledger_changed(user.client_id)  # as usage.record() does
        return event

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    # ── ledger ───────────────────────────────────────────────────────────────────────────────
    def test_every_ledger_row_records_the_key_used(self):
        with gateway.using_key("org:%d" % self.org.pk):
            event = usage.record(user=self.analyst, provider="litellm", model="gpt-test", task="chat",
                                 prompt_tokens=10, completion_tokens=5)
        self.assertEqual(event.metadata["key"], f"org:{self.org.pk}")
        event = usage.record(user=self.analyst, provider="litellm", model="gpt-test", task="chat", prompt_tokens=1)
        self.assertEqual(event.metadata["key"], "platform")

    # ── reporting ────────────────────────────────────────────────────────────────────────────
    def test_spend_by_user_key_model_and_day_for_a_custom_range(self):
        self.spend(self.analyst)                                   # 600*10 + 400*20 per million = USD 0.014
        self.spend(self.analyst, key=f"org:{self.org.pk}")
        self.spend(self.manager, days_ago=40)                       # outside the range below
        self.spend(self.outsider)                                   # another organisation
        today = timezone.localdate()
        r = self.api(self.ceo).get(f"/api/v1/home-analytics/ai-spend/?start={(today - dt.timedelta(days=6)).isoformat()}&end={today.isoformat()}")
        self.assertEqual(r.status_code, 200, r.content)
        d = r.json()
        self.assertEqual(d["totals"]["tokens"], 2000)
        self.assertEqual((d["totals"]["input_tokens"], d["totals"]["output_tokens"]), (1200, 800))
        self.assertEqual(d["by_model"][0]["input_tokens"], 1200)
        self.assertEqual(d["series"][-1]["output_tokens"], 800)
        self.assertEqual(d["totals"]["usd"], "0.03")
        self.assertEqual(len(d["series"]), 7)
        self.assertEqual(d["series"][-1]["tokens"], 2000)
        by_user = {u["email"]: u for u in d["by_user"]}
        self.assertEqual(by_user["analyst@gateway.test"]["tokens"], 2000)
        self.assertEqual(by_user["manager@gateway.test"]["tokens"], 0)
        self.assertNotIn("outsider@other.test", by_user)
        by_key = {k["key_id"]: k for k in d["by_key"]}
        self.assertEqual(by_key["platform"]["tokens"], 1000)
        self.assertEqual(by_key[f"org:{self.org.pk}"]["tokens"], 1000)
        self.assertEqual(d["by_model"][0]["model"], "gpt-test")
        self.assertTrue(d["can_manage"])

    def test_presets_and_unpriced_models(self):
        self.spend(self.analyst, model="local-llama")
        self.spend(self.analyst, days_ago=1)
        d = self.api(self.ceo).get("/api/v1/home-analytics/ai-spend/?range=today").json()
        self.assertEqual(d["range"]["label"], "Today")
        self.assertEqual(d["totals"]["tokens"], 1000)
        self.assertEqual(d["totals"]["unpriced_tokens"], 1000)
        self.assertEqual(d["totals"]["usd"], "0.00")
        self.assertEqual(self.api(self.ceo).get("/api/v1/home-analytics/ai-spend/?range=forever").status_code, 400)
        self.assertEqual(self.api(self.ceo).get("/api/v1/home-analytics/ai-spend/?start=2020-01-01&end=2026-01-01").status_code, 400)

    def test_who_may_see_and_change(self):
        self.assertEqual(self.api(self.analyst).get("/api/v1/home-analytics/ai-spend/").status_code, 403)
        r = self.api(self.manager).get("/api/v1/home-analytics/ai-spend/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["can_manage"])
        body = {"scope": "user", "user_id": self.analyst.pk, "period": "day", "unit": "usd", "amount": "5"}
        self.assertEqual(self.api(self.manager).put("/api/v1/home-analytics/ai-spend/limits/", body, format="json").status_code, 403)
        self.assertEqual(self.api(self.ceo).put("/api/v1/home-analytics/ai-spend/limits/", body, format="json").status_code, 200)
        # Never across organisations.
        foreign = {**body, "user_id": self.outsider.pk}
        self.assertEqual(self.api(self.ceo).put("/api/v1/home-analytics/ai-spend/limits/", foreign, format="json").status_code, 400)
        limit = AISpendLimit.objects.get()
        self.assertEqual(self.api(self.outsider).patch(f"/api/v1/home-analytics/ai-spend/limits/{limit.pk}/",
                                                       {"enabled": False}, format="json").status_code, 404)

    # ── limits and enforcement ───────────────────────────────────────────────────────────────
    def test_person_limit_blocks_and_toggle_switches_it_off(self):
        api = self.api(self.ceo)
        r = api.put("/api/v1/home-analytics/ai-spend/limits/", {"scope": "user", "user_id": self.analyst.pk,
                    "period": "day", "unit": "tokens", "amount": "1500"}, format="json")
        limit_id = r.json()["id"]
        self.assertTrue(gateway.check(self.analyst)[0])
        self.spend(self.analyst, tokens=1600, prompt=1000, completion=600)
        allowed, why = gateway.check(self.analyst)
        self.assertFalse(allowed)
        self.assertIn("daily AI limit", why)
        self.assertTrue(gateway.check(self.manager)[0])              # only that person
        api.patch(f"/api/v1/home-analytics/ai-spend/limits/{limit_id}/", {"enabled": False}, format="json")
        self.assertTrue(gateway.check(self.analyst)[0])
        api.patch(f"/api/v1/home-analytics/ai-spend/limits/{limit_id}/", {"enabled": True, "amount": "5000"}, format="json")
        self.assertTrue(gateway.check(self.analyst)[0])
        row = next(u for u in api.get("/api/v1/home-analytics/ai-spend/?range=today").json()["by_user"]
                   if u["user_id"] == self.analyst.pk)
        self.assertEqual(row["limit"]["spent_tokens"], 1600)
        self.assertEqual(row["limit"]["state"], "ok")

    def test_key_limit_only_blocks_calls_on_that_key_and_resets_each_period(self):
        org_key = f"org:{self.org.pk}"
        self.api(self.ceo).put("/api/v1/home-analytics/ai-spend/limits/", {"scope": "key", "key_id": org_key,
                               "period": "month", "unit": "usd", "amount": "0.01"}, format="json")
        self.spend(self.manager, key=org_key, days_ago=45)            # last month: does not count
        self.assertTrue(gateway.check(self.analyst, org_key)[0])
        self.spend(self.manager, key=org_key)                         # USD 0.014 this month
        self.assertFalse(gateway.check(self.analyst, org_key)[0])
        self.assertTrue(gateway.check(self.analyst, "platform")[0])

    def test_shared_call_path_refuses_when_a_limit_is_used_up(self):
        from grid_documents_interrogation.utils import _openai_chat_completion
        AISpendLimit.objects.create(tenant=self.org, scope="user", user=self.analyst, period="day", unit="tokens", amount=10)
        self.spend(self.analyst, tokens=50, prompt=30, completion=20)
        client = mock.Mock()
        with usage.acting_as(self.analyst), self.assertRaises(gateway.GatewayBlocked):
            _openai_chat_completion(client, model="gpt-test", messages=[], max_tokens=10, temperature=0, timeout=5)
        client.chat.completions.create.assert_not_called()

    def test_organisation_budget_still_applies(self):
        from .models import AIBudget
        AIBudget.objects.create(tenant=self.org, monthly_usd=Decimal("0.01"), hard_stop=True)
        self.spend(self.manager)
        allowed, why = gateway.check(self.analyst)
        self.assertFalse(allowed)
        self.assertIn("budget", why)


class PricingTests(TestCase):
    """USD comes from input and output tokens: admin price, else LiteLLM's registry, else self-hosted = 0."""

    def setUp(self):
        cache.clear()
        cache.set("ai-prices:proxy-aliases", {}, 60)   # no LiteLLM proxy in tests

    def test_registry_prices_input_and_output_tokens_separately(self):
        from .agents.cost import price_for
        price = price_for("litellm", "openai/gpt-4o-mini")
        self.assertEqual(price.input_usd_per_million, Decimal("0.15"))
        self.assertEqual(price.output_usd_per_million, Decimal("0.6"))
        self.assertIn("LiteLLM", price.source)
        self.assertIsNotNone(price_for("litellm", "claude-sonnet-4-5"))

    def test_admin_price_wins_and_self_hosted_is_free_and_unknown_is_unpriced(self):
        from .agents.cost import price_for
        ModelPrice.objects.create(provider="*", model="gpt-4o-mini", input_usd_per_million=Decimal("1"),
                                  output_usd_per_million=Decimal("2"))
        self.assertEqual(price_for("openai", "gpt-4o-mini").input_usd_per_million, Decimal("1"))
        self.assertEqual(price_for("ollama", "qwen2.5:0.5b-instruct").input_usd_per_million, Decimal("0"))
        self.assertIsNone(price_for("litellm", "made-up-model-xyz"))

    def test_proxy_alias_resolves_to_the_model_behind_it(self):
        from .agents.cost import price_for
        cache.set("ai-prices:proxy-aliases", {"house-default": "deepseek/deepseek-chat"}, 60)
        price = price_for("litellm", "house-default")
        self.assertEqual(price.input_usd_per_million, Decimal("0.28"))

    def test_spend_report_uses_registry_prices(self):
        org = Client.objects.create(name="Priced firm")
        exec_user = make_user("cfo@priced.test", org, groups=["Executive"])
        EventLog.objects.create(user=exec_user, tenant=org, event_type=EventType.OTHER, service_type=ServiceType.PAYABLE,
                                tokens_used=2_000_000, metadata={"provider": "litellm", "model": "gpt-4o-mini", "task": "chat",
                                                                 "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000})
        c = APIClient(); c.force_authenticate(exec_user)
        d = c.get("/api/v1/home-analytics/ai-spend/?range=today").json()
        self.assertEqual(d["totals"]["usd"], "0.75")                 # 1M x 0.15 + 1M x 0.60
        self.assertEqual(d["totals"]["unpriced_tokens"], 0)

    def test_refresh_keeps_only_prices_and_rejects_a_broken_registry(self):
        from .agents import pricing
        good = {f"m{i}": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "x"} for i in range(600)}
        good["sample_spec"] = {"input_cost_per_token": 9}
        response = mock.Mock(status_code=200)
        response.raise_for_status = mock.Mock()
        response.raw.read.return_value = __import__("json").dumps(good).encode()
        with mock.patch("requests.get", return_value=response):
            self.assertEqual(pricing.refresh(), 600)
        self.assertEqual(pricing.registry()["m1"], [1.0, 2.0, "x"])
        response.raw.read.return_value = b'{"only": {"input_cost_per_token": 1}}'
        with mock.patch("requests.get", return_value=response), self.assertRaises(ValueError):
            pricing.refresh()
        self.assertIn("m1", pricing.registry())                     # previous prices kept
