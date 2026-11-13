"""
Endpoints the redesigned application depends on: matters, personal activity,
dashboard layout, Insights reports, model discovery and Assistant prompts.
Run: python manage.py test core.tests
"""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import access
from core.models import BulkJob, File, Matter
from core.tests.test_tenancy import make_file, make_user
from custom_authentication.models import Client


def api(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@override_settings(ELASTICSEARCH_DSL_AUTOSYNC=False, ELASTICSEARCH_DSL_AUTO_REFRESH=False)
class MatterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm_a = Client.objects.create(name="Firm A")
        cls.firm_b = Client.objects.create(name="Firm B")
        cls.a1 = make_user("a1@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a2 = make_user("a2@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a_admin = make_user("admin@firm-a.test", cls.firm_a, staff=True, groups=["Admin"])
        cls.b1 = make_user("b1@firm-b.test", cls.firm_b, groups=["Client"])

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # legacy-adoption throttle lives in the shared cache

    def _create(self, user, name="Project Atlas", **extra):
        return api(user).post("/api/v1/core/projects/create/", {"name": name, **extra}, format="json")

    def test_create_and_list(self):
        r = self._create(self.a1, practice_area="Private equity and M&A")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["name"], "Project Atlas")
        listed = api(self.a1).get("/api/v1/core/projects/").json()
        self.assertEqual([m["name"] for m in listed["results"]], ["Project Atlas"])
        self.assertEqual(listed["data"][0]["project_name"], "Project Atlas")  # older screens

    def test_duplicate_name_in_same_firm_is_rejected(self):
        self._create(self.a1)
        self.assertEqual(self._create(self.a2).status_code, 409)

    def test_same_name_in_another_firm_is_independent(self):
        self._create(self.a1)
        self.assertEqual(self._create(self.b1).status_code, 201)

    def test_path_characters_rejected(self):
        for bad in ("../etc", "a/b", "x\\y"):
            self.assertEqual(self._create(self.a1, name=bad).status_code, 400, bad)

    def test_other_firm_cannot_see_or_open_matter(self):
        mid = self._create(self.a1).json()["id"]
        self.assertEqual(api(self.b1).get("/api/v1/core/projects/").json()["count"], 0)
        self.assertEqual(api(self.b1).get(f"/api/v1/core/projects/{mid}/").status_code, 404)
        self.assertEqual(api(self.b1).post(f"/api/v1/core/projects/{mid}/upload/").status_code, 404)

    def test_colleague_not_on_team_cannot_open_matter(self):
        mid = self._create(self.a1).json()["id"]
        self.assertEqual(api(self.a2).get(f"/api/v1/core/projects/{mid}/").status_code, 404)

    def test_tenant_admin_sees_all_firm_matters(self):
        self._create(self.a1)
        self.assertEqual(api(self.a_admin).get("/api/v1/core/projects/").json()["count"], 1)

    def test_team_member_gets_access_to_matter_documents(self):
        mid = self._create(self.a1).json()["id"]
        f = make_file(self.a1, "spa.pdf")
        f.project_id = "Project Atlas"
        f.save()
        self.assertFalse(access.can_access_file(self.a2, f))
        r = api(self.a1).post(f"/api/v1/core/projects/{mid}/members/", {"user_ids": [self.a2.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(access.can_access_file(self.a2, f))
        self.assertFalse(access.can_access_file(self.a2, f, "delete"))  # team members cannot delete
        docs = api(self.a2).get(f"/api/v1/core/projects/{mid}/documents/").json()["results"]
        self.assertEqual([d["filename"] for d in docs], ["spa.pdf"])

    def test_cannot_add_user_from_another_firm(self):
        mid = self._create(self.a1).json()["id"]
        r = api(self.a1).post(f"/api/v1/core/projects/{mid}/members/", {"user_ids": [self.b1.id]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Matter.objects.get(id=mid).members.filter(id=self.b1.id).exists())

    def test_only_owner_or_admin_can_rename(self):
        mid = self._create(self.a1).json()["id"]
        api(self.a1).post(f"/api/v1/core/projects/{mid}/members/", {"user_ids": [self.a2.id]}, format="json")
        self.assertEqual(api(self.a2).patch(f"/api/v1/core/projects/{mid}/", {"name": "X"}, format="json").status_code, 403)
        r = api(self.a_admin).patch(f"/api/v1/core/projects/{mid}/", {"name": "Project Atlas II"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_rename_moves_documents_and_jobs(self):
        mid = self._create(self.a1).json()["id"]
        f = make_file(self.a1, "nda.pdf")
        File.objects.filter(id=f.id).update(project_id="Project Atlas")
        BulkJob.objects.create(user=self.a1, job_type="pe_classify", project_id="Project Atlas", service_id="s")
        api(self.a1).patch(f"/api/v1/core/projects/{mid}/", {"name": "Atlas"}, format="json")
        self.assertEqual(File.objects.get(id=f.id).project_id, "Atlas")
        self.assertTrue(BulkJob.objects.filter(project_id="Atlas").exists())

    def test_legacy_project_ids_become_matters(self):
        f = make_file(self.a1, "old.pdf")
        File.objects.filter(id=f.id).update(project_id="Old deal")
        names = [m["name"] for m in api(self.a1).get("/api/v1/core/projects/").json()["results"]]
        self.assertIn("Old deal", names)
        self.assertNotIn("Old deal", [m["name"] for m in api(self.b1).get("/api/v1/core/projects/").json()["results"]])

    def test_upload_into_matter(self):
        mid = self._create(self.a1).json()["id"]
        pdf = SimpleUploadedFile("Share Purchase.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        with mock.patch("core.views.queue_post_upload_processing") as queued, \
                override_settings(MEDIA_ROOT=self._tmp()):
            r = api(self.a1).post(f"/api/v1/core/projects/{mid}/upload/", {"file": [pdf]}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["uploaded"], 1)
        self.assertTrue(queued.called)
        self.assertTrue(File.objects.filter(user=self.a1, project_id="Project Atlas").exists())

    def test_empty_files_are_rejected(self):
        mid = self._create(self.a1).json()["id"]
        empty = SimpleUploadedFile("blank.pdf", b"", content_type="application/pdf")
        with override_settings(MEDIA_ROOT=self._tmp()):
            r = api(self.a1).post(f"/api/v1/core/projects/{mid}/upload/", {"file": [empty]}, format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["files"][0]["status"], "rejected")

    def test_closed_matter_rejects_uploads(self):
        mid = self._create(self.a1).json()["id"]
        api(self.a1).patch(f"/api/v1/core/projects/{mid}/", {"status": "closed"}, format="json")
        pdf = SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf")
        r = api(self.a1).post(f"/api/v1/core/projects/{mid}/upload/", {"file": [pdf]}, format="multipart")
        self.assertEqual(r.status_code, 400)

    def test_list_is_paginated(self):
        for i in range(30):
            Matter.objects.create(client=self.firm_a, name=f"Deal {i:02d}", created_by=self.a1).members.add(self.a1)
        r = api(self.a1).get("/api/v1/core/projects/?page=2&page_size=25").json()
        self.assertEqual(r["count"], 30)
        self.assertEqual(r["total_pages"], 2)
        self.assertEqual(len(r["results"]), 5)
        self.assertEqual(api(self.a1).get("/api/v1/core/projects/?page_size=5000").json()["page_size"], 100)

    def test_list_counts_documents_per_matter(self):
        mid = self._create(self.a1).json()["id"]
        for n in ("a.pdf", "b.pdf"):
            f = make_file(self.a1, n)
            File.objects.filter(id=f.id).update(project_id="Project Atlas")
        row = next(m for m in api(self.a1).get("/api/v1/core/projects/").json()["results"] if m["id"] == mid)
        self.assertEqual(row["document_count"], 2)

    def test_services_are_validated_and_saved(self):
        self.assertEqual(self._create(self.a1, services=["no-such-service"]).status_code, 400)
        from core.service_catalog import ALL_SERVICES
        st = ALL_SERVICES[0]["service_type"]
        r = self._create(self.a1, name="With services", services=[st, st])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([s["service_type"] for s in r.json()["services"]], [st])

    def test_documents_endpoint_is_paginated_and_searchable(self):
        mid = self._create(self.a1).json()["id"]
        for i in range(7):
            f = make_file(self.a1, f"doc{i}.pdf")
            File.objects.filter(id=f.id).update(project_id="Project Atlas")
        r = api(self.a1).get(f"/api/v1/core/projects/{mid}/documents/?page_size=3").json()
        self.assertEqual((r["count"], r["total_pages"], len(r["results"])), (7, 3, 3))
        r = api(self.a1).get(f"/api/v1/core/projects/{mid}/documents/?search=doc6").json()
        self.assertEqual([d["filename"] for d in r["results"]], ["doc6.pdf"])

    def test_team_member_can_run_on_colleagues_documents(self):
        from core.service_catalog import ALL_SERVICES
        svc = next(s for s in ALL_SERVICES
                   if s.get("handler_job_type") and "pdf" in (s.get("accepted_extensions") or ["pdf"]))
        mid = self._create(self.a1).json()["id"]
        f = make_file(self.a1, "nda.pdf")
        File.objects.filter(id=f.id).update(project_id="Project Atlas", extension="pdf")
        api(self.a1).post(f"/api/v1/core/projects/{mid}/members/", {"user_ids": [self.a2.id]}, format="json")
        with mock.patch("core.tasks_bulk.execute_bulk_job.delay") as delay,                 mock.patch("core.bulk_registry.get_bulk_handler", return_value=object()),                 self.captureOnCommitCallbacks(execute=True):
            r = api(self.a2).post(f"/api/v1/core/projects/{mid}/run/", {"service_type": svc["service_type"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["total_files"], 1)
        delay.assert_called_once()
        jobs = api(self.a1).get(f"/api/v1/core/projects/{mid}/jobs/").json()
        self.assertEqual(jobs["count"], 1)
        self.assertEqual(jobs["results"][0]["run_by"]["email"], self.a2.email)

    def test_other_firm_cannot_reach_matter_endpoints(self):
        mid = self._create(self.a1).json()["id"]
        for path in ("documents/", "jobs/"):
            self.assertEqual(api(self.b1).get(f"/api/v1/core/projects/{mid}/{path}").status_code, 404, path)
        self.assertEqual(api(self.b1).post(f"/api/v1/core/projects/{mid}/run/", {"service_type": "x"},
                                           format="json").status_code, 404)

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


@override_settings(ELASTICSEARCH_DSL_AUTOSYNC=False, ELASTICSEARCH_DSL_AUTO_REFRESH=False)
class ActivityLayoutReportsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm_a = Client.objects.create(name="Firm A")
        cls.firm_b = Client.objects.create(name="Firm B")
        cls.a1 = make_user("a1@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a_admin = make_user("admin@firm-a.test", cls.firm_a, staff=True, groups=["Admin"])
        cls.b1 = make_user("b1@firm-b.test", cls.firm_b, groups=["Client"])
        make_file(cls.a1, "a1.pdf")
        make_file(cls.b1, "b1.pdf")
        BulkJob.objects.create(user=cls.a1, job_type="ca_pii_redaction", project_id="Claim", service_id="s",
                               status="Completed", total_files=3, processed_files=3)
        BulkJob.objects.create(user=cls.b1, job_type="pe_classify", project_id="Other", service_id="s")

    def test_activity_is_own_only_and_paginated(self):
        r = api(self.a1).get("/api/v1/auth/user/activity/?page=1&page_size=1").json()
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["total_pages"], 2)
        r2 = api(self.a1).get("/api/v1/auth/user/activity/?page=1&page_size=20").json()
        names = {e["metadata"].get("filename") for e in r2["results"]}
        self.assertIn("a1.pdf", names)
        self.assertNotIn("b1.pdf", names)

    def test_activity_requires_sign_in(self):
        self.assertIn(APIClient().get("/api/v1/auth/user/activity/").status_code, (401, 403))

    def test_dashboard_layout_roundtrip_and_isolation(self):
        url = "/api/v1/home-analytics/dashboard-layout/?project_id=p1"
        self.assertIsNone(api(self.a1).get(url).json()["layout"])
        self.assertEqual(api(self.a1).put(url, {"layout": [{"id": "w1"}]}, format="json").status_code, 200)
        api(self.a1).put(url, {"ui_prefs": {"theme": "light"}}, format="json")
        got = api(self.a1).get(url).json()
        self.assertEqual(got["layout"], [{"id": "w1"}])
        self.assertEqual(got["ui_prefs"], {"theme": "light"})
        self.assertIsNone(api(self.b1).get(url).json()["layout"])
        self.assertIsNone(api(self.a1).get("/api/v1/home-analytics/dashboard-layout/").json()["layout"])

    def test_dashboard_layout_validates(self):
        url = "/api/v1/home-analytics/dashboard-layout/"
        self.assertEqual(api(self.a1).put(url, {"layout": "nope"}, format="json").status_code, 400)
        self.assertEqual(api(self.a1).put(url, {}, format="json").status_code, 400)

    def test_reports_scoped_to_firm(self):
        r = api(self.a1).get("/api/v1/service-analytics/reports/?page=1&page_size=10").json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["results"][0]["category"], "pii")
        self.assertEqual(r["categories"][0]["id"], "pii")
        self.assertEqual(api(self.b1).get("/api/v1/service-analytics/reports/").json()["total"], 1)
        self.assertEqual(api(self.a_admin).get("/api/v1/service-analytics/reports/").json()["total"], 1)

    def test_reports_category_filter(self):
        r = api(self.a1).get("/api/v1/service-analytics/reports/?category=due_diligence").json()
        self.assertEqual(r["total"], 0)


class ModelDiscoveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])

    url = "/api/v1/grid-documents-interrogation/proxy/models/"

    def test_requires_sign_in(self):
        self.assertIn(APIClient().post(self.url, {"provider": "litellm"}, format="json").status_code, (401, 403))

    def test_unsupported_provider(self):
        self.assertEqual(api(self.user).post(self.url, {"provider": "nope"}, format="json").status_code, 400)

    @mock.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://litellm:4000", "LITELLM_API_KEY": "sk-server"})
    @mock.patch("core.llm_providers.requests.get")
    def test_litellm_lists_models_with_server_key(self, get):
        get.return_value = mock.Mock(status_code=200, json=lambda: {"data": [{"id": "gpt-4o"}, {"id": "claude-x"}]})
        r = api(self.user).post(self.url, {"provider": "litellm"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([m["id"] for m in r.json()["data"]], ["gpt-4o", "claude-x"])
        called_url = get.call_args[0][0]
        self.assertEqual(called_url, "http://litellm:4000/v1/models")
        self.assertEqual(get.call_args[1]["headers"]["Authorization"], "Bearer sk-server")

    @mock.patch.dict("os.environ", {"LITELLM_BASE_URL": "http://litellm:4000", "LLM_ALLOWED_HOSTS": ""})
    @mock.patch("core.llm_providers.requests.get")
    def test_untrusted_endpoint_falls_back_to_server_default(self, get):
        get.return_value = mock.Mock(status_code=200, json=lambda: {"data": []})
        api(self.user).post(self.url, {"provider": "litellm", "endpoint": "http://169.254.169.254/latest"}, format="json")
        self.assertTrue(get.call_args[0][0].startswith("http://litellm:4000/"))

    @mock.patch.dict("os.environ", {"OLLAMA_URL": "http://ollama:11434"})
    @mock.patch("core.llm_providers.requests.get")
    def test_ollama_shape(self, get):
        get.return_value = mock.Mock(status_code=200, json=lambda: {"models": [{"name": "llama3"}]})
        r = api(self.user).post(self.url, {"provider": "ollama"}, format="json")
        self.assertEqual(r.json()["models"][0]["name"], "llama3")
        self.assertEqual(get.call_args[0][0], "http://ollama:11434/api/tags")

    @mock.patch("core.llm_providers.requests.get", side_effect=__import__("requests").ConnectionError("down"))
    def test_unreachable_server_is_502(self, _get):
        r = api(self.user).post(self.url, {"provider": "litellm"}, format="json")
        self.assertEqual(r.status_code, 502)

    def test_catalog_services_carry_prompts(self):
        from core.prompt_templates import prompt_for
        from core.service_catalog import ALL_SERVICES

        self.assertTrue(all(prompt_for(s) for s in ALL_SERVICES))
        self.assertIn("page or clause", prompt_for({"service_type": "pe-extract-risk-clauses"}))


class PracticeAreaDashboardTests(TestCase):
    """Endpoints the platform_check sweep found crashing."""

    @classmethod
    def setUpTestData(cls):
        from core.tests.test_tenancy import token_for
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])
        cls.app, cls.tok = token_for(cls.user)

    def _get(self, path):
        c = APIClient()
        return c.get(path, HTTP_AUTHORIZATION=f"Bearer {self.tok.token}", HTTP_X_CLIENT_ID=self.app.client_id)

    def test_portfolio_compliance(self):
        r = self._get("/api/v1/private-equity/portfolio-compliance/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()["total_covenants"], 0)

    def test_risk_heatmap(self):
        r = self._get("/api/v1/private-equity/risk-heatmap/")
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r.json()["deals"], [])

    def test_my_subscription(self):
        r = self._get("/api/v1/cost/subscriptions/me/")
        self.assertLess(r.status_code, 500, r.content[:300])
