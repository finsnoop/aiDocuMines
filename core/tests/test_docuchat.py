"""
DocuChat (the Assistant page): topics, questions and the database tools stay inside
the caller's own data, and nothing can be pointed at the platform or internal hosts.
The model, DNS and database drivers are mocked.
"""
import socket
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from grid_documents_interrogation import safety
from grid_documents_interrogation.models import DatabaseConnection, Query, Topic

API = "/api/v1/grid-documents-interrogation"


def addr(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


def public_dns(host, *a, **k):
    return addr("93.184.216.34")


def make_doc(owner, name):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(filename=name, filepath=f"/x/{owner.id}/{name}", user=owner, run=run,
                               project_id="Atlas", service_id="Review", md5_hash=f"{owner.id}-{name}",
                               content="Notice is thirty days.")


class DocuChatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other = Client.objects.create(name="Firm B")
        cls.alice = make_user("alice@a.test", cls.firm, groups=["Client"])
        cls.eve = make_user("eve@b.test", cls.other, groups=["Client"])
        cls.doc = make_doc(cls.alice, "nda.pdf")
        cls.eve_doc = make_doc(cls.eve, "eve.pdf")
        with mock.patch("socket.getaddrinfo", public_dns):
            cls.eve_conn = DatabaseConnection.objects.create(
                name="eve db", database_type="postgres", host="db.example.com", port=5432,
                username="u", password="p", database_name="d", owner=cls.eve)

    def setUp(self):
        cache.clear()
        self.c = APIClient()
        self.c.force_authenticate(self.alice)

    # ── Topics ──────────────────────────────────────────────────────────────
    def test_start_topic_and_attach_detach_own_document(self):
        r = self.c.post(f"{API}/topics/start-interrogation/", {"project_id": "Atlas", "service_id": "Review",
                                                               "name": "x" * 500}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        tid = r.json()["topic_id"]
        self.assertEqual(len(Topic.objects.get(pk=tid).name), 100)
        self.assertEqual(self.c.post(f"{API}/topics/{tid}/attach_file/", {"file_id": self.doc.id}, format="json").status_code, 200)
        self.assertEqual(self.c.post(f"{API}/topics/{tid}/attach_file/", {"file_id": self.eve_doc.id}, format="json").status_code, 404)
        self.assertEqual(self.c.post(f"{API}/topics/{tid}/detach_file/", {"file_id": self.doc.id}, format="json").status_code, 200)
        self.assertFalse(Topic.objects.get(pk=tid).files.exists())
        self.assertTrue(File.objects.filter(pk=self.doc.id).exists())

    def test_cannot_link_someone_elses_saved_connection(self):
        r = self.c.post(f"{API}/topics/", {"name": "t", "project_id": "A", "service_id": "B",
                                           "db_connection_id": self.eve_conn.id}, format="json")
        self.assertEqual(r.status_code, 400, r.content)

    def test_other_users_topics_are_invisible(self):
        t = Topic.objects.create(name="eve", project_id="P", service_id="S", user=self.eve)
        self.assertEqual(self.c.get(f"{API}/topics/{t.id}/summary/").status_code, 404)
        self.assertEqual(self.c.delete(f"{API}/topics/{t.id}/").status_code, 404)

    # ── Questions ───────────────────────────────────────────────────────────
    def test_raw_query_create_is_refused(self):
        t = Topic.objects.create(name="t", project_id="P", service_id="S", user=self.alice)
        r = self.c.post(f"{API}/queries/", {"topic": t.id, "file": self.eve_doc.id, "query_text": "q",
                                            "response_text": "a"}, format="json")
        self.assertEqual(r.status_code, 405)

    def test_asking_about_an_unreadable_file_is_404(self):
        t = Topic.objects.create(name="t", project_id="P", service_id="S", user=self.alice)
        t.files.add(self.doc)
        with mock.patch("grid_documents_interrogation.views.process_file_query") as task:
            r = self.c.post(f"{API}/queries/ask/", {"topic_id": t.id, "query_text": "q", "file_id": self.eve_doc.id},
                            format="json")
        self.assertEqual(r.status_code, 404)
        task.delay.assert_not_called()

    def test_ask_cleans_model_settings(self):
        t = Topic.objects.create(name="t", project_id="P", service_id="S", user=self.alice)
        t.files.add(self.doc)
        with mock.patch("grid_documents_interrogation.views.process_file_query") as task:
            task.delay.return_value.get.return_value = "Thirty days."
            r = self.c.post(f"{API}/queries/ask/", {
                "topic_id": t.id, "query_text": "Notice?", "file_id": self.doc.id,
                "llm_config": {"provider": "ollama", "model": "llama3", "endpoint": "http://169.254.169.254/latest",
                               "temperature": 99}}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cfg = task.delay.call_args.kwargs["llm_config"]
        self.assertNotIn("169.254", cfg["endpoint"])
        self.assertLessEqual(cfg["temperature"], 1.5)

    def test_deleting_a_message_never_deletes_the_document(self):
        t = Topic.objects.create(name="t", project_id="P", service_id="S", user=self.alice)
        q = Query.objects.create(topic=t, user=self.alice, file=self.doc, query_text="q", response_text="a")
        self.assertEqual(self.c.delete(f"{API}/queries/{q.id}/delete_chat/").status_code, 200)
        self.assertTrue(File.objects.filter(pk=self.doc.id).exists())

    # ── Database tools ──────────────────────────────────────────────────────
    def _post(self, path, body):
        return self.c.post(f"{API}/{path}", body, format="json")

    def test_platform_and_internal_databases_are_refused(self):
        cases = [
            ("sqlite:////app/db.sqlite3", None),
            ("postgresql://u:p@localhost/x", addr("127.0.0.1")),
            ("postgresql://u:p@10.0.0.5/x", addr("10.0.0.5")),
            ("mysql://u:p@metadata.internal/x", addr("169.254.169.254")),
            ("postgresql://u:p@db.example.com/x?host=/var/run/postgresql", addr("192.168.1.4")),
        ]
        for conn, dns in cases:
            with mock.patch("socket.getaddrinfo", return_value=dns or addr("127.0.0.1")), \
                    mock.patch("grid_documents_interrogation.db_meta_views.fetch_tables") as tables:
                r = self._post("db/tables/", {"connection_string": conn})
            self.assertEqual(r.status_code, 400, conn)
            tables.assert_not_called()
            self.assertNotIn("u:p", r.content.decode())

    @override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
                                              "HOST": "platform-db"}})
    def test_platform_database_host_is_refused_even_if_public(self):
        with mock.patch("socket.getaddrinfo", public_dns):
            r = self._post("db/test-connection/", {"connection_string": "postgresql://a:b@platform-db/aidoc"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("platform", r.json()["error"])

    def test_public_database_is_allowed_and_errors_are_generic(self):
        with mock.patch("socket.getaddrinfo", public_dns), \
                mock.patch("grid_documents_interrogation.db_meta_views.fetch_tables", return_value=["users"]):
            r = self._post("db/tables/", {"connection_string": "postgresql://u:secret@db.example.com/x"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["tables"], ["users"])
        with mock.patch("socket.getaddrinfo", public_dns), \
                mock.patch("grid_documents_interrogation.db_meta_views.fetch_tables",
                           side_effect=RuntimeError("FATAL password=secret host 10.1.1.1")):
            r = self._post("db/tables/", {"connection_string": "postgresql://u:secret@db.example.com/x"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("secret", r.content.decode())

    def test_someone_elses_saved_connection_cannot_be_used(self):
        with mock.patch("socket.getaddrinfo", public_dns), \
                mock.patch("grid_documents_interrogation.db_meta_views.fetch_tables") as tables:
            r = self._post("db/tables/", {"connection_id": self.eve_conn.id})
        self.assertEqual(r.status_code, 400)
        tables.assert_not_called()

    def test_saving_an_internal_or_sqlite_connection_is_refused(self):
        with mock.patch("socket.getaddrinfo", return_value=addr("10.2.3.4")):
            r = self._post("connections/", {"name": "x", "database_type": "postgres", "host": "db.corp",
                                            "port": 5432, "username": "u", "password": "p", "database_name": "d"})
        self.assertEqual(r.status_code, 400)
        r = self._post("connections/", {"name": "x", "database_type": "sqlite", "host": "h", "port": 1,
                                        "username": "u", "password": "p", "database_name": "/etc/passwd"})
        self.assertEqual(r.status_code, 400)

    def test_saved_connection_password_is_never_returned(self):
        with mock.patch("socket.getaddrinfo", public_dns):
            r = self._post("connections/", {"name": "x", "database_type": "postgres", "host": "db.example.com",
                                            "port": 5432, "username": "u", "password": "hunter2", "database_name": "d"})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertNotIn("hunter2", r.content.decode())
        self.assertNotIn("hunter2", self.c.get(f"{API}/connections/").content.decode())

    @mock.patch.dict("os.environ", {"DB_CONNECT_ALLOWED_NETWORKS": "10.20.0.0/16"})
    def test_operator_can_allow_an_internal_network(self):
        with mock.patch("socket.getaddrinfo", return_value=addr("10.20.1.9")), \
                mock.patch("grid_documents_interrogation.db_meta_views.fetch_tables", return_value=[]):
            self.assertEqual(self._post("db/tables/", {"connection_string": "postgresql://u:p@warehouse/x"}).status_code, 200)

    @mock.patch.dict("os.environ", {"ASSISTANT_DB_ENABLED": "false"})
    def test_database_tools_can_be_switched_off(self):
        self.assertEqual(self._post("db/tables/", {"connection_string": "postgresql://u:p@db.example.com/x"}).status_code, 403)

    # ── Document facets for the topic picker ───────────────────────────────
    def test_facets_by_project_only_show_readable_services(self):
        r = self.c.get("/api/v1/documents/table/facets/", {"project_id": "Atlas"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["services"], ["Review"])
        self.assertEqual(r.json()["matters"], ["Atlas"])


class SafeSelectTests(TestCase):
    def test_only_single_read_only_selects_pass(self):
        self.assertTrue(safety.safe_select("SELECT name FROM users WHERE id > 3").endswith("LIMIT 500"))
        self.assertTrue(safety.safe_select("```sql\nWITH x AS (SELECT 1) SELECT * FROM x;\n```"))
        for bad in ("DELETE FROM users", "SELECT 1; DROP TABLE users", "SELECT * INTO t2 FROM users",
                    "UPDATE users SET a=1", "SELECT 1 -- comment", "select pg_sleep(1); select 1", ""):
            with self.assertRaises(safety.UnsafeTarget, msg=bad):
                safety.safe_select(bad)
