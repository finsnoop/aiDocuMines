"""Preview by default; explicit offline all-tenant document purge."""
import json

from django.core.management.base import BaseCommand, CommandError
from core import document_purge as purge


class Command(BaseCommand):
    help = "Preview or delete all platform documents/artifacts, preserving accounts and configuration. Stop all document producers first."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="Perform deletion; omitted means read-only preview.")
        parser.add_argument("--all-tenants", action="store_true", help="Explicitly acknowledge all organisations.")
        parser.add_argument("--storage-root", action="append", default=[], help="Additional explicit owned root for recorded local paths; never a filesystem root.")
        parser.add_argument("--include-node-caches", action="store_true", help="Clear only the allowlisted document cache tables in --node-database.")
        parser.add_argument("--node-database", default="default", help="Configured Django database alias containing Node caches.")

    def handle(self, *args, **options):
        execute = options["execute"]
        if execute and not options["all_tenants"]:
            raise CommandError("Execution requires --execute --all-tenants. Stop web, workers, beat, file monitor and connector producers first.")
        report = {"mode": "execute" if execute else "preview", "scope": "all_tenants", "complete": False,
                  "preserved": ["accounts", "settings", "workflow/template definitions", "integration configurations", "business/matter definitions", "backups", "remote originals"],
                  "errors": []}
        try:
            roots = purge.owned_roots(options["storage_root"])
            paths, missing, issues = purge.inventory_paths(roots)
            report.update(storage_roots=[str(root) for root in roots], files=len(paths), missing_files=missing,
                          file_bytes=sum(path.stat().st_size for path in paths if path.exists()),
                          rows={label: query.count() for label, query in purge.deletion_queries()},
                          preserved_business_runs=purge.model("core.Run").objects.count() - purge.removable_runs().count())
            report["errors"].extend(issues)
            keys = purge.document_cache_keys()
            report["document_cache_keys"] = len(keys)
            node = purge.node_inventory(options["node_database"]) if options["include_node_caches"] else {}
            report["node_cache_tables"] = node
            report["node_cache_scope"] = options["node_database"] if options["include_node_caches"] else "not_requested"
            if options["include_node_caches"] and not {"files", "ocr", "anonymization", "translation"} <= set(node):
                report["errors"].append("Requested Node document-cache tables are missing; choose the correct configured --node-database alias.")
            from core.holds import active_holds
            holds = active_holds().count()
            report["active_legal_holds"] = holds
            if holds:
                report["errors"].append(f"{holds} legal hold(s) are active. Documents under legal hold must be preserved; "
                                        "release the holds before purging.")
            search, errors = purge.search_inventory()
            report["search"] = search
            report["errors"].extend(errors)
            if report["errors"]:
                raise purge.PurgeBlocked("Preflight failed; no documents were deleted.")
            if not execute:
                report["complete"] = True
                self.stdout.write(json.dumps(report, sort_keys=True))
                return
            # Validate every original path again; never follow a replacement
            # symlink. An unavailable file leaves database rows for a retry.
            removed = 0
            for path in sorted(paths):
                try:
                    if purge.unlink_document(path, roots):
                        removed += 1
                except (OSError, purge.PurgeBlocked) as exc:
                    report["errors"].append(f"File deletion failed: {path} ({type(exc).__name__}).")
            report["files_deleted"] = removed
            if report["errors"]:
                raise purge.PurgeBlocked("Filesystem cleanup incomplete; database rows retained for retry.")
            report["rows_deleted"] = purge.delete_document_rows()
            if node:
                purge.clear_node_tables(options["node_database"], node)
            purge.clear_document_cache(keys)
            report["search"], errors = purge.search_inventory(execute=True)
            report["errors"].extend(errors)
            report["remaining_files"] = purge.model("core.File").objects.count()
            if report["remaining_files"]:
                report["errors"].append("New document rows appeared; stop every producer and retry.")
            report["complete"] = not report["errors"]
        except (purge.PurgeBlocked, OSError, KeyError) as exc:
            report["errors"].append(str(exc))
        except Exception as exc:
            report["errors"].append(f"Purge failed/incomplete ({type(exc).__name__}); inspect server logs and rerun the preview.")
        self.stdout.write(json.dumps(report, sort_keys=True))
        if not report["complete"]:
            raise CommandError("Document purge incomplete; see the JSON report. Producers must remain stopped until resolved.")
