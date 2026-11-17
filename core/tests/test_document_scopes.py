"""Document-table Assistant scopes and lazy Sidebar link identity/ACL regressions."""
import hashlib
from unittest import mock

from django.core.cache import cache
from django.http import Http404
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from core.assistant import compose_prompt, resolve_context
from core.models import File, Run
from core.tests.test_assistant import ask, make_doc
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess, FileAccessEntry, FileFolderLink, Folder

TABLE = "/api/v1/documents/table/"


class DocumentScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Scope firm")
        cls.other_firm = Client.objects.create(name="Other scope firm")
        cls.alice = make_user("scope-alice@test.invalid", cls.firm, groups=["Client"])
        cls.bob = make_user("scope-bob@test.invalid", cls.firm, groups=["Client"])
        cls.eve = make_user("scope-eve@test.invalid", cls.other_firm, groups=["Client"])
        cls.admin = make_user("scope-admin@test.invalid", cls.firm, staff=True, groups=["Admin"])
        cls.folder = Folder.objects.create(name="Contracts", user=cls.alice, project_id="Atlas", service_id="legal")
        cls.child = Folder.objects.create(name="Amendments", parent=cls.folder, user=cls.alice,
                                          project_id="Atlas", service_id="legal")
        cls.private_folder = Folder.objects.create(name="Private alias", user=cls.bob,
                                                   project_id="Atlas", service_id="legal")
        cls.nda = make_doc(cls.alice, "nda.pdf", "Thirty days notice.", service="legal")
        cls.amendment = make_doc(cls.alice, "amendment.docx", "Amended payment amount.", service="legal")
        cls.other = make_doc(cls.alice, "budget.pdf", "Other project budget.", project="Budget", service="finance")
        cls.shared = make_doc(cls.bob, "shared.pdf", "Shared legal advice.", service="legal")
        cls.secret = make_doc(cls.eve, "secret.pdf", "Foreign tenant secret.", service="legal")
        cls.deliverable = make_doc(cls.alice, "generated.pdf", "A generated output.")
        cls.archived = make_doc(cls.alice, "archived.pdf", "Archived text.")
        File.objects.filter(pk=cls.deliverable.pk).update(document_type="deliverable")
        File.objects.filter(pk__in=[cls.nda.pk, cls.other.pk, cls.shared.pk, cls.secret.pk]).update(extension="PDF")
        File.objects.filter(pk=cls.amendment.pk).update(extension="docx")
        cls.nda_link = FileFolderLink.objects.create(file=cls.nda, folder=cls.folder)
        cls.child_link = FileFolderLink.objects.create(file=cls.amendment, folder=cls.child)
        cls.shared_link = FileFolderLink.objects.create(file=cls.shared, folder=cls.private_folder)
        FileAccessEntry.objects.create(file_link=cls.shared_link, user=cls.alice, can_read=True)
        FileFolderLink.objects.create(file=cls.archived, folder=cls.folder, is_trashed=True)
        cls.hidden_alias = FileFolderLink.objects.create(file=cls.nda, folder=cls.private_folder)

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(user=self.alice)

    def test_filters_resolve_the_same_ordered_documents_as_the_table(self):
        cases = [
            {}, {"scope": "mine"}, {"scope": "shared"}, {"search": "nda"},
            {"project_id": "Atlas", "service_id": "legal", "extension": "pdf", "ordering": "name"},
            {"folder": str(self.folder.pk), "ordering": "-name"},
            {"folder": str(self.child.pk), "search": "amendment"},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                table = self.client.get(TABLE, filters)
                self.assertEqual(table.status_code, 200, table.content)
                ctx = resolve_context(self.alice, {"type": "documents", "filters": filters})
                self.assertEqual([f.pk for f in ctx.files], [r["id"] for r in table.json()["results"]])
                self.assertEqual(ctx.total_in_scope, table.json()["count"])
        self.assertEqual({f.pk for f in resolve_context(self.alice, {"type": "documents"}).files},
                         {self.nda.pk, self.amendment.pk, self.other.pk, self.shared.pk})

    def test_tenant_admin_scope_still_excludes_other_tenants(self):
        ctx = resolve_context(self.admin, {"type": "documents"})
        self.assertIn(self.shared.pk, [f.pk for f in ctx.files])
        self.assertNotIn(self.secret.pk, [f.pk for f in ctx.files])

    def test_selection_preserves_order_and_overrides_filters(self):
        ctx = resolve_context(self.alice, {"type": "documents", "file_ids": [self.other.pk, self.nda.pk],
                                           "filters": {"search": "nothing"}, "label": "Untrusted label"})
        self.assertEqual([f.pk for f in ctx.files], [self.other.pk, self.nda.pk])
        self.assertEqual(ctx.label, "2 selected documents")
        self.assertEqual(ctx.scope_mode, "selection")

    def test_explicit_selection_fails_as_a_whole_for_foreign_missing_or_archived_documents(self):
        for bad in (self.secret.pk, self.archived.pk, self.deliverable.pk, 999999):
            with self.subTest(bad=bad), self.assertRaises(Http404):
                resolve_context(self.alice, {"type": "documents", "file_ids": [self.nda.pk, bad]})

    def test_malformed_scopes_are_clear_validation_errors(self):
        cases = [
            [], {"type": "documents", "file_ids": []}, {"type": "documents", "file_ids": "1"},
            {"type": "documents", "file_ids": [True]}, {"type": "documents", "file_ids": [1.2]},
            {"type": "documents", "file_ids": ["-1"]}, {"type": "documents", "filters": []},
            {"type": "documents", "filters": {"scope": "tenant"}},
            {"type": "documents", "filters": {"user_id": self.eve.pk}},
            {"type": "documents", "filters": {"client_id": self.other_firm.pk}},
            {"type": "documents", "filters": {"folder": "bad-id"}},
            {"type": "documents", "filters": {"search": {"$regex": "x"}}},
            {"type": "documents", "filters": {"ordering": "user__email"}},
            {"type": "documents", "data": {"invented_total": 1000}},
            {"type": "documents", "file_ids": [self.nda.pk] * 201},
        ]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                resolve_context(self.alice, spec)

    def test_invalid_filters_do_not_silently_broaden_table(self):
        for filters in ({"scope": "typo"}, {"folder": "invalid"}, {"ordering": "filename"}):
            self.assertEqual(self.client.get(TABLE, filters).status_code, 400)

    @mock.patch("document_operations.document_query.folder_ids_below")
    def test_private_location_is_rejected_before_traversing_even_with_a_readable_file(self, below):
        filters = {"folder": str(self.private_folder.pk)}
        self.assertEqual(self.client.get(TABLE, filters).status_code, 404)
        with self.assertRaises(Http404):
            resolve_context(self.alice, {"type": "documents", "filters": filters})
        below.assert_not_called()

    def test_long_client_label_does_not_block_a_valid_selection(self):
        ctx = resolve_context(self.alice, {"type": "documents", "file_ids": [self.nda.pk], "label": "x" * 255})
        self.assertEqual(ctx.label, self.nda.filename)

    @mock.patch("grid_documents_interrogation.utils.dispatch_to_llm")
    def test_empty_filter_result_does_not_call_ai_or_web(self, llm):
        with mock.patch("core.views_assistant.web_search") as web:
            r = ask(self.alice, {"type": "documents", "filters": {"search": "no matching file"}}, web=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("No accessible documents match", r.json()["answer"])
        self.assertEqual(r.json()["context"]["files_in_scope"], 0)
        self.assertEqual(r.json()["sources"], [])
        llm.assert_not_called()
        web.assert_not_called()

    @mock.patch("core.assistant.semantic_first", return_value=[])
    @mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", return_value="Thirty days [S1].")
    def test_documents_answers_use_document_text_and_report_scope(self, llm, _semantic):
        r = ask(self.alice, {"type": "documents", "filters": {"search": "nda"}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("Thirty days notice", llm.call_args[0][1])
        self.assertEqual(r.json()["sources"][0]["file_id"], self.nda.pk)
        self.assertEqual(r.json()["context"]["coverage"], "complete")
        self.assertIn("Search: nda", r.json()["context"]["label"])

    @mock.patch("core.assistant.semantic_first", return_value=[])
    @mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", return_value="Answer [S1].")
    def test_passage_truncation_is_reported_as_partial_even_when_all_files_were_scanned(self, llm, _semantic):
        File.objects.filter(pk=self.nda.pk).update(content="A long notice clause with evidence. " * 2000)
        r = ask(self.alice, {"type": "documents", "file_ids": [self.nda.pk]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["context"]["files_read"], 1)
        self.assertEqual(r.json()["context"]["coverage"], "partial")
        self.assertIn("Coverage is partial", llm.call_args[0][0])

    def _many_documents(self, count):
        run = Run.objects.create(user=self.alice, status="Completed")
        return File.objects.bulk_create([
            File(user=self.alice, run=run, filename=f"bulk-{i:03}.pdf", filepath=f"/x/bulk-{i}.pdf",
                 project_id="Large", service_id="legal", content=f"Clause {i}",
                 md5_hash=hashlib.md5(f"scope-bulk-{i}".encode()).hexdigest()) for i in range(count)
        ])

    def test_scope_spans_pages_but_bounds_reading_and_discloses_partial_coverage(self):
        self._many_documents(205)
        filters = {"project_id": "Large", "ordering": "name"}
        table = self.client.get(TABLE, {**filters, "page_size": 10})
        ctx = resolve_context(self.alice, {"type": "documents", "filters": filters})
        self.assertEqual(len(table.json()["results"]), 10)
        self.assertEqual(ctx.total_in_scope, 205)
        self.assertEqual(len(ctx.files), 200)
        self.assertEqual(ctx.files[0].filename, "bulk-000.pdf")
        query, _ = compose_prompt("Summarise", ctx, [], [])
        self.assertIn("Coverage is partial", query)
        self.assertIn("Do not infer collection-wide totals", query)

    def test_selection_keeps_more_than_twenty_documents(self):
        files = self._many_documents(25)
        ids = [f.pk for f in reversed(files)]
        ctx = resolve_context(self.alice, {"type": "documents", "file_ids": ids})
        self.assertEqual([f.pk for f in ctx.files], ids)
        self.assertEqual(ctx.total_in_scope, 25)

    def test_filter_count_is_bounded_and_marks_lower_bound(self):
        with mock.patch("document_operations.document_query.COUNT_CAP", 2):
            ctx = resolve_context(self.alice, {"type": "documents"})
        self.assertEqual(ctx.total_in_scope, 2)
        self.assertTrue(ctx.count_is_capped)

    def test_action_context_returns_link_key_not_core_file_key(self):
        r = self.client.get(f"{TABLE}{self.nda.pk}/context/")
        self.assertEqual(r.status_code, 200, r.content)
        node = r.json()["node"]
        self.assertEqual(node["key"], str(self.nda_link.pk))
        self.assertEqual(node["data"]["file_id"], self.nda.pk)
        self.assertEqual(node["data"]["folder_id"], str(self.folder.pk))
        self.assertTrue(r.json()["permissions"]["rename"])
        self.assertNotIn("/x/", node["download_url"])

    def test_action_context_honours_exact_location_and_private_aliases(self):
        r = self.client.get(f"{TABLE}{self.nda.pk}/context/", {"folder": str(self.private_folder.pk)})
        self.assertEqual(r.status_code, 404)
        # A descendant shown by a parent filter retains its own safe link.
        r = self.client.get(f"{TABLE}{self.amendment.pk}/context/", {"folder": str(self.folder.pk)})
        self.assertEqual(r.json()["node"]["key"], str(self.child_link.pk))

    def test_read_only_share_does_not_expose_private_folder(self):
        r = self.client.get(f"{TABLE}{self.shared.pk}/context/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(r.json()["node"])
        self.assertTrue(r.json()["permissions"]["read"])
        for action in ("rename", "move", "delete", "share"):
            self.assertFalse(r.json()["permissions"][action])
        self.assertNotIn(str(self.private_folder.pk), str(r.json()))

    def test_folder_grant_allows_safe_node_without_extra_file_permissions(self):
        EffectiveAccess.objects.create(user=self.alice, folder=self.private_folder, can_download=True)
        r = self.client.get(f"{TABLE}{self.shared.pk}/context/")
        self.assertEqual(r.json()["node"]["key"], str(self.shared_link.pk))
        self.assertFalse(r.json()["permissions"]["rename"])

    def test_action_context_rejects_foreign_and_malformed_locations(self):
        self.assertEqual(self.client.get(f"{TABLE}{self.secret.pk}/context/").status_code, 404)
        self.assertEqual(self.client.get(f"{TABLE}{self.nda.pk}/context/", {"folder": "bad"}).status_code, 400)
