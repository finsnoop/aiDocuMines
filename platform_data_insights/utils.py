# platform_data_insights/utils.py

from core.utils import get_logical_path
from core.models import File, Metadata, Run, Storage
from django.db.models import Count, Sum, Avg, Max, Min, Q
from django.utils import timezone
from datetime import timedelta
from django.db.models.functions import TruncDate
from document_anonymizer.models import Anonymize
from document_translation.models import TranslationFile
from grid_documents_interrogation.models import Topic, Query


def calculate_user_insights(user):
    """
    Perform efficient, scalable data aggregation for a single user.
    Returns a dict with useful platform usage insights.
    """

    # 1. File summary counts
    file_qs = File.objects.filter(user=user)
    total_file_count = file_qs.count()
    pending_file_count = file_qs.filter(status="Pending").count()
    failed_file_count = file_qs.filter(status="Failed").count()
    processing_file_count = file_qs.filter(status="Processing").count()

    # 2. Recent files (sample only)
    recent_files = list(
        file_qs.order_by("-created_at")
        .values("id", "filename", "status", "created_at")[:10]
    )
    for f in recent_files:
        f["created_at"] = f["created_at"].isoformat() if f["created_at"] else None

    # 3. Document type distribution
    doc_type_distribution = list(
        file_qs.values("document_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    # 4. Daily volume trend (last 30 days)
    today = timezone.now()
    past_30 = today - timedelta(days=30)
    volume_trend = list(
        file_qs.filter(created_at__gte=past_30)
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    )
    for v in volume_trend:
        v["date"] = v["date"].isoformat()

    # 5. Duplicate detection (MD5)
    duplicate_files = list(
        file_qs.values("md5_hash", "filename")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .order_by("-count")[:10]
    )

    # 6. Encrypted files count
    encrypted_count = Metadata.objects.filter(file__user=user, is_encrypted=True).count()

    # 7. Metadata completeness
    meta_qs = Metadata.objects.filter(file__user=user)
    total_meta = meta_qs.count() or 1  # prevent division by zero

    completeness = {
        "has_author": round(100 * meta_qs.filter(author__isnull=False).count() / total_meta, 2),
        "has_title": round(100 * meta_qs.filter(title__isnull=False).count() / total_meta, 2),
        "has_page_count": round(100 * meta_qs.filter(page_count__gt=0).count() / total_meta, 2),
    }

    metadata_issues = []
    if completeness["has_author"] < 100:
        metadata_issues.append("Some documents are missing authors.")
    if completeness["has_title"] < 100:
        metadata_issues.append("Some documents are missing titles.")
    if completeness["has_page_count"] < 100:
        metadata_issues.append("Some documents have 0 pages.")

    # 8. Storage locations used
    #storages = list(
    #    Storage.objects.filter(user=user)
    #    .values("upload_storage_location", "output_storage_location")
    #    .annotate(count=Count("id"))
    #)

    storages = list(
        Storage.objects.filter(user=user)
        .values("upload_storage_location", "output_storage_location")
        .annotate(count=Count("upload_storage_location"))
    )
    for storage_entry in storages:
        storage_entry["upload_storage_location"] = get_logical_path(storage_entry["upload_storage_location"])
        storage_entry["output_storage_location"] = get_logical_path(storage_entry["output_storage_location"])

    # 9. Average document size
    size_summary = file_qs.aggregate(
        avg_size=Avg("file_size"),
        min_size=Min("file_size"),
        max_size=Max("file_size")
    )
    document_size_summary = {
        "average_kb": round((size_summary["avg_size"] or 0) / 1024, 2),
        "min_kb": round((size_summary["min_size"] or 0) / 1024, 2),
        "max_kb": round((size_summary["max_size"] or 0) / 1024, 2),
    }

    # 10. Top file types by extension
    top_file_types = list(
        file_qs.values("extension")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    # 11. Delayed processing (>1hr)
    delayed_files_count = file_qs.filter(
        status="Processing",
        created_at__lt=timezone.now() - timedelta(hours=1)
    ).count()

    # 12. Run stats
    runs = Run.objects.filter(user=user)
    avg_processing_time = None
    if runs.exists():
        durations = runs.exclude(updated_at__isnull=True).annotate(
            duration=Sum("updated_at") - Sum("created_at")
        )
        times = []
        for r in runs:
            if r.created_at and r.updated_at:
                times.append((r.updated_at - r.created_at).total_seconds())
        if times:
            avg_processing_time = round(sum(times) / len(times), 2)

    total_cost = runs.aggregate(total=Sum("cost"))["total"] or 0

    # 13. Anonymized docs
    anonymized_docs_count = Anonymize.objects.filter(
        original_file__user=user
    ).count()

    # 14. Translated files
    translated_docs_count = TranslationFile.objects.filter(
        original_file__user=user
    ).count()

    # 15. Insights generated (topics + queries)
    topics_count = Topic.objects.filter(user=user).count()
    queries_count = Query.objects.filter(user=user).count()
    insights_count = topics_count + queries_count

    # 16. Modified files (recently updated, not newly created)
    day_ago = timezone.now() - timedelta(days=1)
    modified_files_count = file_qs.filter(
        updated_at__gte=day_ago,
        created_at__lt=day_ago
    ).count()

    # 17. Files at risk: failed + delayed processing
    files_at_risk_count = failed_file_count + delayed_files_count

    return {
        "total_file_count": total_file_count,
        "pending_file_count": pending_file_count,
        "failed_file_count": failed_file_count,
        "processing_file_count": processing_file_count,
        "modified_files_count": modified_files_count,
        "files_at_risk_count": files_at_risk_count,
        "recent_files": recent_files,
        "doc_type_distribution": doc_type_distribution,
        "top_file_types": top_file_types,
        "volume_trend": volume_trend,
        "duplicate_files": duplicate_files,
        "encrypted_docs_count": encrypted_count,
        "anonymized_docs_count": anonymized_docs_count,
        "translated_docs_count": translated_docs_count,
        "insights_count": insights_count,
        "metadata_completeness": completeness,
        "metadata_issues": metadata_issues,
        "storages": storages,
        "document_size_summary": document_size_summary,
        "avg_pages": meta_qs.aggregate(avg=Avg("page_count"))["avg"] or 0,
        "avg_processing_time_seconds": avg_processing_time,
        "delayed_files_count": delayed_files_count,
        "total_cost": round(float(total_cost), 2),
    }

