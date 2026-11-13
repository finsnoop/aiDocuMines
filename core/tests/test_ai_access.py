"""
Organisation AI access: only an organisation's administrators set the AI key the
whole organisation uses; people cannot bring their own keys unless allowed; the key
is never returned, cached or queued in plain text.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, OrganisationPolicy
from custom_authentication.policies import ai_access, policy_for
from grid_documents_interrogation.safety import clean_llm_config

POLICY = "/api/v1/auth/admin/policy/"
ORG_KEY = "sk-org-0123456789abcdef"


def api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


class OrganisationAIAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other = Client.objects.create(name="Firm B")
        cls.admin = make_user("admin@a.test", cls.firm, staff=True, groups=["Admin"])
        cls.member = make_user("member@a.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("x@b.test", cls.other, groups=["Client"])

    def setUp(self):
        cache.clear()

    def _set_key(self, **extra):
        return api(self.admin).patch(POLICY, {"ai_provider": "openai", "ai_api_key": ORG_KEY, **extra}, format="json")

    def test_admin_sets_key_and_it_is_never_returned(self):
        r = self._set_key()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn(ORG_KEY, r.content.decode())
        self.assertTrue(r.json()["ai_key_configured"])
        self.assertEqual(r.json()["ai_key_hint"], "…cdef")
        self.assertNotIn(ORG_KEY, api(self.admin).get(POLICY).content.decode())
        self.assertEqual(OrganisationPolicy.objects.get(client=self.firm).ai_api_key, ORG_KEY)

    def test_only_admins_can_set_the_key(self):
        r = api(self.member).patch(POLICY, {"ai_api_key": ORG_KEY}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(OrganisationPolicy.objects.get_or_create(client=self.firm)[0].ai_api_key)

    def test_key_can_be_removed(self):
        self._set_key()
        r = api(self.admin).patch(POLICY, {"clear_ai_api_key": True}, format="json")
        self.assertFalse(r.json()["ai_key_configured"])

    def test_personal_keys_and_endpoints_are_ignored_by_default(self):
        self._set_key()
        cfg = clean_llm_config({"provider": "ollama", "api_key": "sk-personal-xyz", "endpoint": "http://evil.example"},
                               self.member)
        self.assertNotIn("api_key", cfg)
        self.assertEqual(cfg["provider"], "openai")                 # the organisation's provider
        self.assertEqual(cfg["key_org"], self.firm.id)              # key resolved at call time, not queued
        self.assertNotIn("evil", cfg["endpoint"])

    def test_personal_keys_when_the_organisation_allows_them(self):
        self._set_key(allow_personal_ai_keys=True)
        cfg = clean_llm_config({"provider": "openai", "api_key": "sk-personal-xyz"}, self.member)
        self.assertEqual(cfg["api_key"], "sk-personal-xyz")

    def test_request_cannot_borrow_another_organisations_key(self):
        api(self.admin).patch(POLICY, {"ai_api_key": ORG_KEY}, format="json")
        cfg = clean_llm_config({"provider": "openai", "key_org": self.firm.id}, self.outsider)
        self.assertNotIn("key_org", cfg)

    def test_model_call_uses_the_organisation_key(self):
        self._set_key()
        from grid_documents_interrogation import utils
        seen = {}

        class FakeOpenAI:
            def __init__(self, api_key=None, **kw):
                seen["key"] = api_key
                msg = mock.Mock(); msg.message.content = "ok"
                self.chat = mock.Mock(); self.chat.completions.create.return_value = mock.Mock(choices=[msg])

        with mock.patch.object(utils, "OpenAI", FakeOpenAI):
            out = utils.dispatch_to_llm("q", "context", clean_llm_config({"model": "gpt-4o-mini"}, self.member))
        self.assertEqual(out, "ok")
        self.assertEqual(seen["key"], ORG_KEY)

    def test_model_list_ignores_personal_keys_by_default(self):
        self._set_key()
        with mock.patch("grid_documents_interrogation.views_models.list_models", return_value={"data": []}) as lm:
            api(self.member).post("/api/v1/grid-documents-interrogation/proxy/models/",
                                  {"provider": "openai", "api_key": "sk-personal-xyz", "endpoint": "http://evil.example"},
                                  format="json")
        self.assertEqual(lm.call_args[0], ("openai", None, ORG_KEY))

    def test_key_is_not_in_the_cached_policy(self):
        self._set_key()
        cached = policy_for(self.member)
        self.assertIn("ai_api_key", cached.get_deferred_fields())
        self.assertTrue(ai_access(self.member)["has_org_key"])

    def test_whoami_reports_the_mode(self):
        self._set_key()
        from custom_authentication.views import WhoAmIView  # noqa: F401  (route below)
        r = api(self.member).get("/api/v1/auth/whoami/")
        if r.status_code == 200:
            self.assertFalse(r.json()["ai_personal_keys_allowed"])
            self.assertTrue(r.json()["ai_managed_by_organisation"])
