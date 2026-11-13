"""
Find and fix files missing from the search indexes.

    python manage.py repair_search_indexes            # report only
    python manage.py repair_search_indexes --fix      # re-queue missing files
    python manage.py repair_search_indexes --rebuild-es        # recreate the ES index (1 shard, 0 replicas) and re-index
    python manage.py repair_search_indexes --unblock-es        # clear the read-only block ES sets when the disk filled up

Runs nightly via Celery beat (document_search.repair_search_indexes).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Report or repair files missing from Milvus and Elasticsearch."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true", help="Re-queue every missing file.")
        parser.add_argument("--limit", type=int, help="Only check the newest N files.")
        parser.add_argument("--rebuild-es", action="store_true", help="Delete and rebuild the Elasticsearch index.")
        parser.add_argument("--unblock-es", action="store_true", help="Clear index.blocks.read_only_allow_delete.")

    def handle(self, *args, **opts):
        from document_search import milvus_store
        from document_search.tasks import find_unindexed, repair_search_indexes

        if opts["unblock_es"]:
            from elasticsearch_dsl.connections import connections
            connections.get_connection().indices.put_settings(
                index="files", body={"index.blocks.read_only_allow_delete": None})
            self.stdout.write(self.style.SUCCESS("Cleared the read-only block on the 'files' index."))

        if opts["rebuild_es"]:
            from file_elasticsearch.utils import force_reindex
            force_reindex()
            self.stdout.write(self.style.SUCCESS("Elasticsearch index rebuilt."))

        try:
            self.stdout.write(f"Milvus: {milvus_store.stats()}")
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Milvus unavailable: {exc}"))
            return

        report = find_unindexed(opts.get("limit"))
        es = report["es_missing"]
        self.stdout.write(f"Checked {report['checked']} files: {len(report['milvus_missing'])} missing from Milvus, "
                          + (f"{len(es)} missing from Elasticsearch." if es is not None else "Elasticsearch unreachable."))
        if opts["fix"]:
            self.stdout.write(self.style.SUCCESS(f"Queued: {repair_search_indexes(opts.get('limit'))}"))
        elif report["milvus_missing"] or es:
            self.stdout.write("Run again with --fix to re-index them.")
