"""Offline, all-tenant document purge. No account/configuration or remote deletion."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db import connections, transaction
from django.db.models import Q

# Only document instances/processing results, never entire application schemas.
DELETE_MODELS = (
    "core.File", "document_operations.FileVersion", "document_versioning.DocumentVersion",
    "document_versioning.VersionManifest", "document_versioning.VersionArchiveRecord",
    "document_ocr.OCRRun", "document_ocr.OCRStorage", "document_anonymizer.AnonymizationRun",
    "document_anonymizer.AnonymizationStorage", "document_anonymizer.AnonymizationStats",
    "document_translation.TranslationRun", "document_translation.TranslationStorage",
    "document_classification.ClusteringRun", "document_classification.ClusterStorage",
    "document_classification.SubClusterRun", "document_structures.DocumentStructureRun",
    "document_redlining.RedliningRun", "document_automation.AutomationRun", "document_workflows.WorkflowRun",
    "document_search.SearchQueryLog", "grid_documents_interrogation.Query",
    "home_dash_analytics.DocumentIntelligence", "home_dash_analytics.IntelligenceRun",
    "home_dash_analytics.HomeAnalyticsSnapshot", "platform_data_insights.UserInsights",
    "integrations.ConnectorJob", "email_management.EmailAttachment", "custom_authentication.Document",
    "core.BulkJob", "core.EndpointResponseTable",
    "private_equity.DocumentClassification", "private_equity.RiskClause", "private_equity.FindingsReport",
    "class_actions.IntakeForm", "class_actions.EvidenceDocument", "class_actions.PIIRedaction",
    "class_actions.ExhibitPackage", "ip_litigation.PatentDocument", "ip_litigation.PriorArtDocument",
    "labor_employment.CommunicationMessage", "labor_employment.WageHourAnalysis",
    "labor_employment.PolicyComparison", "labor_employment.EEOCPacket",
    "private_equity.ServiceExecution", "class_actions.ServiceExecution", "labor_employment.ServiceExecution",
    "ip_litigation.ServiceExecution", "regulatory_compliance.ServiceExecution", "core.Storage",
)
# File descendants with paths also need inventory before their cascade deletes.
PATH_MODELS = DELETE_MODELS + (
    "document_ocr.OCRFile", "document_ocr.OCRBatch", "document_anonymizer.Anonymize",
    "document_anonymizer.DeAnonymize", "document_translation.TranslationFile",
    "document_redlining.RedliningResult", "document_automation.AutomationResult",
    "document_classification.ClusterFile", "private_equity.ServiceOutput", "class_actions.ServiceOutput",
    "labor_employment.ServiceOutput", "ip_litigation.ServiceOutput", "regulatory_compliance.ServiceOutput",
)
PATH_NAMES = {"filepath", "file_path", "path", "archived_path"}
# Orphans are swept only in these application-owned document directories.
DOCUMENT_DIRS = (
    "uploads", "versions", "zips", "ocr", "anonymized", "deanonymized", "translations",
    "generated", "exports", "redlining", "structured", "structures", "clustering",
    "email_attachments", "pe_service_outputs", "ca_service_outputs", "le_service_outputs",
    "ip_service_outputs", "rc_service_outputs", "proposal_docs",
)
NODE_TABLES = {
    "files": {"id_user", "id_node", "data_type", "metadata", "node"},
    "ocr": {"id_original_file", "run_id", "ocr_option", "ocr_file"},
    "anonymization": {"id_original_file", "run_id", "selectedfiletype", "anonymization_file"},
    "translation": {"id_original_file", "run_id", "to_lang", "translated_file"},
    "activitylogs": {"id_original_file", "details", "activitylogs_file"},
}
CACHE_PATTERNS = (
    "text-preparation:*", "pair-assistant:*", "search:v*", "search:index_version", "similar-groups:*",
    "explicit-ocr-source:*", "explicit-anonymization-source:*", "document_operation_context:*", "task_owner:*",
    "owned_task:document_operation:*", "owned_task:risk:*", "owned_task:anonymization_stats:*",
    "owned_task:private_equity:*", "owned_task:search:*",
)


class PurgeBlocked(ValueError):
    pass


def model(label):
    try:
        return apps.get_model(label)
    except LookupError:
        return None


def deletion_queries():
    return [(label, cls.objects.all()) for label in DELETE_MODELS if (cls := model(label)) is not None]


def owned_roots(extra=()):
    roots = []
    for value in [settings.MEDIA_ROOT, *extra]:
        candidate = Path(value).absolute()
        if any(part.is_symlink() for part in [candidate, *candidate.parents]):
            raise PurgeBlocked(f"Storage root contains a symlink: {candidate}")
        resolved = candidate.resolve()
        base = Path(settings.BASE_DIR).resolve()
        if resolved == Path(resolved.anchor) or resolved == base or resolved in base.parents or resolved == Path.home().resolve():
            raise PurgeBlocked(f"Refusing broad storage root: {resolved}")
        if resolved not in roots:
            roots.append(resolved)
    return roots


def checked_path(value, roots):
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raise PurgeBlocked("A recorded document uses remote storage; remove it through its configured storage backend, not this local purge.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(settings.MEDIA_ROOT) / candidate
    candidate = candidate.absolute()
    if any(part.is_symlink() for part in [candidate, *candidate.parents]):
        raise PurgeBlocked(f"Refusing symlink path: {candidate}")
    resolved = candidate.resolve()
    if not any(resolved != root and resolved.is_relative_to(root) for root in roots):
        raise PurgeBlocked(f"Document path is outside approved storage roots: {candidate}")
    if resolved.exists() and not resolved.is_file():
        raise PurgeBlocked(f"Expected a regular document file: {resolved}")
    return resolved


def inventory_paths(roots):
    paths, missing, issues = set(), 0, []
    for label in dict.fromkeys(PATH_MODELS):
        cls = model(label)
        if cls is None:
            continue
        for field in cls._meta.concrete_fields:
            is_file = field.get_internal_type() == "FileField"
            if not is_file and not (field.name in PATH_NAMES or field.name.endswith(("_filepath", "_path", "_storage_location"))):
                continue
            if not is_file and field.get_internal_type() not in {"CharField", "TextField"}:
                continue
            for value in cls.objects.exclude(**{field.name: None}).exclude(**{field.name: ""}).values_list(field.name, flat=True).iterator(chunk_size=500):
                try:
                    path = checked_path(value, roots)
                    if path:
                        paths.add(path)
                except PurgeBlocked as exc:
                    issues.append(f"{label}.{field.name}: {exc}")
    media = Path(settings.MEDIA_ROOT).resolve()
    sweeps = [media / name for name in DOCUMENT_DIRS]
    for directory in sweeps:
        if directory.is_symlink():
            issues.append(f"Refusing symlink document directory: {directory}")
            continue
        if not directory.exists():
            continue
        for current, directories, files in os.walk(directory, followlinks=False, onerror=lambda error: issues.append(f"Cannot inventory document directory: {error.filename}")):
            for name in directories:
                if (Path(current) / name).is_symlink():
                    issues.append(f"Refusing nested symlink: {Path(current) / name}")
            for name in files:
                try:
                    path = checked_path(Path(current) / name, roots)
                    if path:
                        paths.add(path)
                except PurgeBlocked as exc:
                    issues.append(str(exc))
    missing = sum(not path.exists() for path in paths)
    return paths, missing, issues


def unlink_document(path, roots):
    """Use anchored directory descriptors: a swapped parent cannot escape roots."""
    path = checked_path(path, roots)
    if not path.exists():
        return False
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise PurgeBlocked("Safe execution requires Linux directory-descriptor and O_NOFOLLOW support.")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise PurgeBlocked(f"Document changed to a non-regular file: {path}")
        os.unlink(path.name, dir_fd=descriptor)
        return True
    finally:
        os.close(descriptor)


def node_inventory(alias):
    connection = connections[alias]
    found = {}
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        for name, required in NODE_TABLES.items():
            if name not in tables:
                continue
            columns = {column.name.lower() for column in connection.introspection.get_table_description(cursor, name)}
            if not required <= columns:
                raise PurgeBlocked(f"Node cache table {name} has an unexpected schema; refusing deletion.")
            table = connection.ops.quote_name(name)
            condition = ' WHERE id_original_file IS NOT NULL' if name == "activitylogs" else ''
            cursor.execute(f"SELECT COUNT(*) FROM {table}{condition}")
            found[name] = cursor.fetchone()[0]
    return found


def clear_node_tables(alias, tables):
    connection = connections[alias]
    with transaction.atomic(using=alias), connection.cursor() as cursor:
        for name in tables:
            if name not in NODE_TABLES:
                raise PurgeBlocked("Unexpected Node table.")
            table = connection.ops.quote_name(name)
            condition = ' WHERE id_original_file IS NOT NULL' if name == "activitylogs" else ''
            cursor.execute(f"DELETE FROM {table}{condition}")


def document_cache_keys():
    if not hasattr(cache, "iter_keys"):
        raise PurgeBlocked("The cache backend cannot enumerate document namespaces; configure the deployment Redis cache before purging.")
    return {key for pattern in CACHE_PATTERNS for key in cache.iter_keys(pattern)}


def clear_document_cache(keys):
    from celery import current_app
    for key in keys:
        if key.startswith(("owned_task:", "task_owner:")):
            current_app.backend.forget(key.rsplit(":", 1)[-1])
    if keys:
        cache.delete_many(keys)


def search_inventory(execute=False):
    """Only the configured document index/collection. Never create missing stores."""
    result, errors = {}, []
    try:
        from elasticsearch_dsl.connections import connections as es_connections
        from core.elastic_indexes import FileIndex
        name = FileIndex._index._name
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name or ""):
            raise PurgeBlocked("The configured document Elasticsearch index is not a single safe name.")
        client = es_connections.get_connection()
        count = 0
        if client.indices.exists(index=name):
            if execute:
                response = client.delete_by_query(index=name, body={"query": {"match_all": {}}}, refresh=True,
                                                 wait_for_completion=True, conflicts="abort")
                if response.get("failures") or response.get("version_conflicts"):
                    raise PurgeBlocked("Elasticsearch reported incomplete document deletion.")
            count = client.count(index=name)["count"]
            if execute and count:
                raise PurgeBlocked("Elasticsearch still contains document records.")
        result["elasticsearch"] = {"index": name, "remaining" if execute else "documents": count}
    except Exception as exc:
        errors.append(f"Elasticsearch cleanup unavailable/incomplete ({type(exc).__name__}).")
    try:
        from pymilvus import Collection, connections as milvus_connections, utility
        from document_search import config
        name, alias = config.COLLECTION_NAME, "document_purge"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
            raise PurgeBlocked("The configured document Milvus collection is not a single safe name.")
        milvus_connections.connect(alias=alias, host=config.MILVUS_HOST, port=str(config.MILVUS_PORT), timeout=10)
        count = 0
        if utility.has_collection(name, using=alias):
            collection = Collection(name, using=alias)
            if not {"file_id", "chunk_text", "vector"} <= {field.name for field in collection.schema.fields}:
                raise PurgeBlocked("The Milvus collection does not have the document schema.")
            if execute:
                collection.load()
                collection.delete("file_id >= 0")
                collection.flush()
                count = collection.query(expr="file_id >= 0", output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"]
                if count:
                    raise PurgeBlocked("Milvus still contains document vectors.")
            else:
                count = collection.num_entities
        result["milvus"] = {"collection": name, "remaining" if execute else "estimated_vectors": count}
    except Exception as exc:
        errors.append(f"Milvus cleanup unavailable/incomplete ({type(exc).__name__}).")
    return result, errors


def removable_runs():
    """Keep business/config parents; deleting them could remove connector settings."""
    run = model("core.Run")
    known = {label.lower() for label in DELETE_MODELS} | {"document_structures.documentstructurerun"}
    protected = Q(pk__in=[])
    for relation in run._meta.related_objects:
        if relation.related_model._meta.label_lower not in known:
            protected |= Q(**{relation.name + "__isnull": False})
    return run.objects.exclude(protected).distinct()


def delete_document_rows():
    from django.db.models.signals import post_delete
    from core import signals
    file_model = model("core.File")
    # Search stores are cleared synchronously below; avoid filling the broker
    # with per-file cleanup jobs while producers/workers are stopped.
    disconnected = post_delete.disconnect(signals.delete_file_from_es_index, sender=file_model)
    deleted = {}
    try:
        with transaction.atomic():
            for label, queryset in deletion_queries():
                _, counts = queryset.delete()
                for name, count in counts.items():
                    deleted[name] = deleted.get(name, 0) + count
            _, counts = removable_runs().delete()
            for name, count in counts.items():
                deleted[name] = deleted.get(name, 0) + count
    finally:
        if disconnected:
            post_delete.connect(signals.delete_file_from_es_index, sender=file_model)
    return deleted
