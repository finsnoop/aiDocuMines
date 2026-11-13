"""
Search indexing must be idempotent and self-healing, and OnlyOffice must be able
to fetch documents with its signed link. Milvus and Elasticsearch are mocked.
"""
import os
import tempfile
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File
from core.tests.test_tenancy import make_file, make_user
from custom_authentication.models import Client


class MilvusHelpersTests(TestCase):
    def test_chunk_hash_is_stable_and_distinct(self):
        from document_search.milvus_store import chunk_hash

        self.assertEqual(chunk_hash("clause 4.2"), chunk_hash("clause 4.2"))
        self.assertNotEqual(chunk_hash("clause 4.2"), chunk_hash("clause 4.3"))
        self.assertTrue(-(2 ** 63) <= chunk_hash("x" * 10000) < 2 ** 63)
        # Known value: must not change between processes or releases.
        self.assertEqual(chunk_hash(""), int.from_bytes(bytes.fromhex("e4a6a0577479b2b4"), "big", signed=True))

    def test_hnsw_ef_always_covers_limit(self):
        from document_search.milvus_store import search_params

        self.assertGreaterEqual(search_params(10)["params"]["ef"], 10)
        self.assertGreaterEqual(search_params(500)["params"]["ef"], 500)


@override_settings(ELASTICSEARCH_DSL_AUTOSYNC=False)
class IndexFileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.f = make_file(self.user, "spa.pdf")
        p = mock.patch.multiple(
            "document_search.tasks",
            compute_chunks=mock.Mock(return_value=(["alpha", "beta"], [[0.1] * 3, [0.2] * 3])),
            _classify_document=mock.Mock(),
        )
        p.start()
        self.addCleanup(p.stop)

    def _run(self, **kw):
        from document_search.tasks import index_file
        return index_file.run(self.f.id, **kw)

    @mock.patch("document_search.tasks._bump_index_version")
    @mock.patch("document_search.milvus_store.replace_file_vectors", return_value=2)
    def test_indexes_once_then_skips(self, replace, _bump):
        self.assertEqual(self._run()["status"], "ok")
        self.assertEqual(self.f.vector_chunks.count(), 2)
        self.assertEqual(self._run()["status"], "skipped")
        self.assertEqual(replace.call_count, 1)

    @mock.patch("document_search.tasks._bump_index_version")
    @mock.patch("document_search.milvus_store.replace_file_vectors", return_value=2)
    def test_force_reindex_replaces_not_duplicates(self, replace, _bump):
        self._run()
        self._run(force=True)
        self.assertEqual(self.f.vector_chunks.count(), 2)  # replaced, not 4
        self.assertEqual(replace.call_count, 2)  # Milvus side deletes then inserts each time

    @mock.patch("document_search.milvus_store.replace_file_vectors", side_effect=RuntimeError("milvus down"))
    def test_milvus_failure_is_not_recorded_as_indexed(self, _replace):
        with self.assertRaises(Exception):
            self._run()
        self.assertEqual(self.f.vector_chunks.count(), 0)  # so the retry / nightly repair indexes it

    @mock.patch("document_search.milvus_store.replace_file_vectors", return_value=2)
    def test_concurrent_run_is_skipped(self, replace):
        from django.core.cache import cache
        cache.add(f"lock:index_file:{self.f.id}", "other-worker", timeout=60)
        self.assertEqual(self._run(force=True)["status"], "in-progress")
        replace.assert_not_called()

    def test_missing_file_is_not_retried(self):
        from document_search.tasks import index_file
        self.assertEqual(index_file.run(987654)["status"], "error")


class SignalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])

    def test_indexing_is_queued_after_commit_not_in_request(self):
        from core import signals

        f = make_file(self.user, "nda.pdf")
        with mock.patch("file_elasticsearch.tasks.es_index_file.delay") as es_delay, \
                mock.patch("document_search.tasks.index_file.delay") as mv_delay, \
                self.captureOnCommitCallbacks(execute=True) as callbacks:
            signals.index_file_to_elasticsearch(File, f, created=True)
            signals.index_file_to_milvus(File, f, created=True)
            es_delay.assert_not_called()  # nothing runs before the transaction commits
        self.assertEqual(len(callbacks), 2)
        es_delay.assert_called_once_with(f.id)
        mv_delay.assert_called_once_with(f.id)

    def test_label_only_saves_do_not_reindex(self):
        from core import signals

        f = make_file(self.user, "nda.pdf")
        with self.captureOnCommitCallbacks() as callbacks:
            signals.index_file_to_elasticsearch(File, f, update_fields={"document_type"})
        self.assertEqual(callbacks, [])

    def test_broker_outage_never_breaks_a_delete(self):
        from core import signals

        f = make_file(self.user, "old.pdf")
        with mock.patch("file_elasticsearch.tasks.es_delete_file.delay", side_effect=ConnectionError("redis down")), \
                mock.patch("document_search.tasks.delete_file_vectors_task.delay", side_effect=ConnectionError("down")), \
                self.captureOnCommitCallbacks(execute=True):
            signals.delete_file_from_es_index(File, f)  # must not raise


class OnlyOfficeSignedDownloadTests(TestCase):
    """The Document Server fetches files without a session; the signed token is the authorisation."""

    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".docx")
        os.write(fd, b"PK-docx-bytes")
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.f = make_file(self.user, "contract.docx", path=self.path)

    def _url(self, token):
        return f"/api/v1/core/onlyoffice/signed-download/?file_id={self.f.id}&token={token}"

    def test_valid_signed_link_works_without_login(self):
        from urllib.parse import parse_qs, urlparse
        from core.onlyoffice_utils import make_signed_download_url

        token = parse_qs(urlparse(make_signed_download_url(str(self.f.id))).query)["token"][0]
        r = APIClient().get(self._url(token))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"PK-docx-bytes")

    def test_forged_or_mismatched_token_is_refused(self):
        from urllib.parse import parse_qs, urlparse
        from core.onlyoffice_utils import make_signed_download_url

        other = make_file(self.user, "other.docx")
        token = parse_qs(urlparse(make_signed_download_url(str(other.id))).query)["token"][0]
        self.assertEqual(APIClient().get(self._url(token)).status_code, 403)   # token for another file
        self.assertEqual(APIClient().get(self._url("forged:123")).status_code, 403)
