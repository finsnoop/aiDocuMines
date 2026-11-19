"""Opt-in verification against private indexes on the local audit search stack."""
import os
import uuid
from unittest import mock, skipUnless

from django.test import SimpleTestCase


@skipUnless(os.getenv("RUN_DOCUMENT_SEARCH_E2E") == "1", "Requires isolated Elasticsearch/Milvus services")
class DocumentPurgeSearchE2ETests(SimpleTestCase):
    def test_real_search_preview_and_delete_leave_other_indexes_untouched(self):
        from elasticsearch_dsl.connections import connections as es_connections
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility
        from core import document_purge
        from core.elastic_indexes import FileIndex
        from document_search import config
        name = "aidoc_purge_e2e_" + uuid.uuid4().hex
        keep = name + "_keep"
        alias = name
        es = es_connections.get_connection()
        connections.connect(alias=alias, host=config.MILVUS_HOST, port=str(config.MILVUS_PORT), timeout=15)
        created_es, created_milvus = [], []
        try:
            for target in [name, keep]:
                es.indices.create(index=target)
                created_es.append(target)
                es.index(index=target, id="1", document={"content": "Synthetic private fixture"}, refresh=True)
                schema = CollectionSchema([
                    FieldSchema("file_id", DataType.INT64, is_primary=True),
                    FieldSchema("chunk_text", DataType.VARCHAR, max_length=128),
                    FieldSchema("vector", DataType.FLOAT_VECTOR, dim=4),
                ])
                collection = Collection(target, schema=schema, using=alias)
                created_milvus.append(target)
                collection.insert([[1], ["Synthetic private fixture"], [[0.1, 0.2, 0.3, 0.4]]])
                collection.flush()
                collection.create_index("vector", {"index_type": "FLAT", "metric_type": "L2", "params": {}})
                collection.load()
            with mock.patch.object(FileIndex._index, "_name", name), mock.patch.object(config, "COLLECTION_NAME", name):
                preview, errors = document_purge.search_inventory()
                self.assertEqual(errors, [], preview)
                self.assertEqual(preview["elasticsearch"]["documents"], 1)
                self.assertEqual(es.count(index=name)["count"], 1)
                self.assertEqual(Collection(name, using=alias).query("file_id >= 0", output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"], 1)
                result, errors = document_purge.search_inventory(execute=True)
                self.assertEqual(errors, [], result)
                self.assertEqual(result["elasticsearch"]["remaining"], 0)
                self.assertEqual(result["milvus"]["remaining"], 0)
                self.assertEqual(es.count(index=keep)["count"], 1)
                self.assertEqual(Collection(keep, using=alias).query("file_id >= 0", output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"], 1)
        finally:
            for target in created_milvus:
                if target.startswith("aidoc_purge_e2e_") and utility.has_collection(target, using=alias):
                    Collection(target, using=alias).drop()
            for target in created_es:
                if target.startswith("aidoc_purge_e2e_"):
                    es.indices.delete(index=target, ignore_unavailable=True)
            connections.disconnect(alias)
