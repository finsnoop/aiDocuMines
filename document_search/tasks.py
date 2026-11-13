"""
document_search.tasks
~~~~~~~~~~~~~~~~~~~

Celery tasks for background indexing & re-indexing.

Highlights
----------
• ONE Milvus collection → memory stays bounded.
• Partition-per-user (`user_<id>`) → zero data-leak risk.
• Idempotent indexing  → re-runs safe with force=True.
• Embeddings cached in VectorChunk → rebuild Milvus anytime.

Dependencies
------------
Celery worker already running in your project (e.g. `celery -A aiDocuMines worker -l info`).

Adjust MILVUS_* or PARTITION_PREFIX in `config.py` if needed.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, List, Tuple

from celery import shared_task
from django.db import transaction
from django.core.cache import cache
from django.db.models import Q
from django.contrib.auth import get_user_model
from datetime import datetime

from core.models import File, Run
from document_search.models import VectorChunk, SearchQueryLog
from document_search.utils import (
    compute_chunks,
    preview_for_file,
    _get_model,
    embed_text,   # used in exec_search
)

# Use the same access helper the views rely on
from document_operations.utils import get_user_accessible_file_ids

# Import report generation utility
from core.utils import generate_and_register_service_report

# ───────────────────────────── Config & constants ───────────────────────────
try:
    from document_search import config as search_config
    MILVUS_HOST = getattr(search_config, "MILVUS_HOST", "localhost")
    MILVUS_PORT = getattr(search_config, "MILVUS_PORT", "19530")
    COLLECTION_NAME = getattr(search_config, "COLLECTION_NAME", "doc_embeddings_v2")
    PARTITION_PREFIX = getattr(search_config, "PARTITION_PREFIX", "user_")
    VECTOR_DIM = getattr(search_config, "VECTOR_DIM", 384)
    BATCH_SZ = getattr(search_config, "MILVUS_INSERT_BATCH", 500)
    MILVUS_INDEX_TYPE = getattr(search_config, "MILVUS_INDEX_TYPE", "HNSW")
    MILVUS_METRIC_TYPE = getattr(search_config, "MILVUS_METRIC_TYPE", "COSINE")
    MILVUS_NLIST = getattr(search_config, "MILVUS_NLIST", 256)
    MILVUS_NPROBE = getattr(search_config, "MILVUS_NPROBE", 16)
    MILVUS_M = getattr(search_config, "MILVUS_M", 16)
    MILVUS_EF_CONSTRUCTION = getattr(search_config, "MILVUS_EF_CONSTRUCTION", 200)
    MILVUS_EF = getattr(search_config, "MILVUS_EF", 64)
    MILVUS_NCENTROIDS = getattr(search_config, "MILVUS_NCENTROIDS", 16384)
except ImportError:
    MILVUS_HOST = "localhost"
    MILVUS_PORT = "19530"
    COLLECTION_NAME = "doc_embeddings_v2"
    PARTITION_PREFIX = "user_"
    VECTOR_DIM = 384
    BATCH_SZ = 500
    MILVUS_INDEX_TYPE = "HNSW"
    MILVUS_METRIC_TYPE = "COSINE"
    MILVUS_NLIST = 256
    MILVUS_NPROBE = 16
    MILVUS_M = 16
    MILVUS_EF_CONSTRUCTION = 200
    MILVUS_EF = 64
    MILVUS_NCENTROIDS = 16384

LOGGER = logging.getLogger(__name__)

# ───────────────────────────── Milvus access ────────────────────────────────
from document_search import milvus_store  # noqa: E402


def _ensure_collection():
    """Loaded collection (kept for callers in views and commands)."""
    return milvus_store.get_collection()


def _partition_name(user_id: int) -> str:
    return f"{PARTITION_PREFIX}{user_id}"


INDEX_VERSION_KEY = "search:index_version"
LOCK_TTL = 15 * 60  # seconds; a crashed worker cannot block a file for longer


# ───────────────────────────── Classification ─────────────────────────────────
CLASSIFICATION_LABELS = {
    # Legal & Compliance
    "Contract": "Legal contract between parties with terms and signatures.",
    "Legal Agreement": "Legal obligations, rights, or terms between parties.",
    "NDA": "Non-disclosure agreement restricting sharing confidential information.",
    "SLA": "Service level agreement with performance standards and responsibilities.",
    "Court Order": "Orders or judgments issued by a court.",
    "Legal Complaint": "Formal legal complaint filed in court.",
    "Terms and Conditions": "Rules and legal agreements for using products or services.",
    "Privacy Policy": "Explains how personal data is collected and used.",
    "Policy Document": "Official rules or guidelines that must be followed.",
    "Permit": "Legal permission granted for specific activities.",
    "License": "Authorization document granting legal permission.",
    "Certificate": "Official document verifying a fact or achievement.",
    "Will": "Estate distribution instructions after death.",
    # Finance & Accounting
    "Financial Report": "Financial results, performance, or analysis.",
    "Income Statement": "Revenue and expenses over a period.",
    "Balance Sheet": "Assets, liabilities, and equity snapshot.",
    "Cash Flow Statement": "Cash inflows and outflows over a period.",
    "Budget": "Planned income and expenses for a period.",
    "Invoice": "Bill for payment with items and totals.",
    "Receipt": "Acknowledgment of payment received.",
    "Bank Statement": "Account transactions and balances.",
    "Audit Report": "Independent financial audit opinion.",
    "Payroll Report": "Employee wages and deductions summary.",
    "Purchase Order": "Authorization to buy goods or services.",
    "Bill of Lading": "Receipt of goods for shipment.",
    "Statement of Work": "Project deliverables, scope, and responsibilities.",
    # Business & Operations
    "Business Proposal": "Proposes plans, services, or products to a client.",
    "Business Plan": "Business strategies, objectives, and forecasts.",
    "RFP Response": "Response to a request for proposal.",
    "SOP": "Standard operating procedure with step-by-step instructions.",
    "Project Report": "Project progress, findings, or results.",
    "Meeting Minutes": "Discussion points and decisions from meetings.",
    "Memo": "Formal internal communication message.",
    "Agenda": "List of topics to be discussed in a meeting.",
    "Checklist": "Tasks or items to complete or verify.",
    "Schedule": "Timeline or plan with dates and times.",
    "Log File": "System, server, or application log entries.",
    "User Manual": "Instructions for using a product or system.",
    "Technical Specification": "Detailed technical requirements and designs.",
    "Runbook": "Operational procedures for incidents or maintenance.",
    "Architecture Diagram": "System architecture documentation overview.",
    # Sales & Marketing
    "Press Release": "Public announcement of news or events.",
    "Brochure": "Marketing or informational pamphlet.",
    "Advertisement": "Promotes products, services, or events.",
    "Price List": "Catalog of products or services with prices.",
    "Statement of Capabilities": "Company capabilities and differentiators.",
    # HR & Talent
    "Resume": "Work experience and skills summary.",
    "Cover Letter": "Letter expressing job interest accompanying a resume.",
    "Offer Letter": "Employment offer details and terms.",
    "Job Description": "Role responsibilities and required qualifications.",
    "Performance Review": "Employee performance evaluation.",
    # Medical & Insurance
    "Medical Report": "Medical or health record details and assessments.",
    "Prescription": "Medication or treatment directive by a clinician.",
    "Lab Result": "Medical or laboratory test outcomes.",
    "Patient Summary": "Patient medical history and conditions.",
    "Insurance Claim": "Request to insurer for reimbursement.",
    "EOB": "Explanation of benefits document from insurer.",
    # Research & Education
    "Research Paper": "Academic research findings and analysis.",
    "White Paper": "Authoritative information or solution on a topic.",
    "Case Study": "Detailed analysis of a specific example.",
    "Thesis": "Lengthy academic dissertation.",
    "Lecture Notes": "Notes from educational lectures.",
    "Transcript": "Verbatim record of spoken words or courses.",
    "Dataset Description": "Metadata and description for datasets.",
    # IT & Security
    "Security Policy": "Information security rules and standards.",
    "Vulnerability Report": "Security weaknesses and remediation.",
    "Penetration Test Report": "Results of simulated attacks and fixes.",
    "Incident Report": "Security incident details and timeline.",
    "Change Request": "Proposed system change and approvals.",
    "Release Notes": "Software release changes and fixes.",
    # Government & Public
    "Notice": "Official information or updates to the public.",
    "Regulatory Filing": "Submission to a regulator or exchange.",
    # Misc
    "FAQ": "Frequently asked questions and answers.",
    "Summary": "Condensed version of longer content.",
    "Newsletter": "Periodic news or updates for readers.",
}

# Filename keyword hints used when no text is extractable (scanned docs etc.)
FILENAME_TYPE_HINTS = [
    ("invoice", "Invoice"),
    ("receipt", "Receipt"),
    ("purchase order", "Purchase Order"),
    ("contract", "Contract"),
    ("agreement", "Legal Agreement"),
    ("non-disclosure", "NDA"),
    ("nda", "NDA"),
    ("sla", "SLA"),
    ("bank statement", "Bank Statement"),
    ("statement", "Bank Statement"),
    ("budget", "Budget"),
    ("payroll", "Payroll Report"),
    ("balance sheet", "Balance Sheet"),
    ("resume", "Resume"),
    ("cover letter", "Cover Letter"),
    ("offer letter", "Offer Letter"),
    ("job description", "Job Description"),
    ("policy", "Policy Document"),
    ("privacy", "Privacy Policy"),
    ("license", "License"),
    ("licence", "License"),
    ("certificate", "Certificate"),
    ("transcript", "Transcript"),
    ("minutes", "Meeting Minutes"),
    ("proposal", "Business Proposal"),
    ("business plan", "Business Plan"),
    ("manual", "User Manual"),
    ("specification", "Technical Specification"),
    ("audit", "Audit Report"),
    ("insurance claim", "Insurance Claim"),
    ("prescription", "Prescription"),
    ("lab result", "Lab Result"),
    ("white paper", "White Paper"),
    ("research", "Research Paper"),
    ("thesis", "Thesis"),
    ("newsletter", "Newsletter"),
    ("press release", "Press Release"),
]


def classify_document_content(embed_model, all_text: str) -> str:
    """Embed whole-doc text and return the closest label from CLASSIFICATION_LABELS."""
    label_texts = list(CLASSIFICATION_LABELS.values())
    label_names = list(CLASSIFICATION_LABELS.keys())
    label_embeddings = embed_model.encode(label_texts)

    from sklearn.metrics.pairwise import cosine_similarity
    scores = cosine_similarity([embed_model.encode([all_text])[0]], label_embeddings)
    best_idx = int(scores.argmax())
    return label_names[best_idx]


def guess_type_from_filename(filename: str):
    """Best-effort document-type guess from filename keywords. Returns None if no hint."""
    name = (filename or "").lower()
    for keyword, label in FILENAME_TYPE_HINTS:
        if keyword in name:
            return label
    return None


def _ocr_pdf_pages(filepath: str, max_pages: int = 10) -> str:
    """
    Rasterize pages of an image-only PDF and OCR them so scanned
    documents can still be classified. Retries at higher DPI when the
    first pass yields nothing.
    """
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except Exception:
        return ""
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        LOGGER.error("OCR unavailable for %s: tesseract binary missing/broken (%s)", filepath, exc)
        return ""
    try:
        for dpi in (150, 300):
            texts = []
            with fitz.open(str(filepath)) as doc:
                for page in list(doc)[:max_pages]:
                    try:
                        pix = page.get_pixmap(dpi=dpi)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        texts.append(pytesseract.image_to_string(img))
                    except Exception:
                        continue
            combined = "\n".join(t for t in texts if t)
            if combined.strip():
                return combined
        return ""
    except Exception as exc:
        LOGGER.warning("PDF OCR fallback failed for %s: %s", filepath, exc)
        return ""



def _bump_index_version() -> None:
    try:
        cache.incr(INDEX_VERSION_KEY)
    except ValueError:
        cache.set(INDEX_VERSION_KEY, 1, timeout=None)
    except Exception:  # cache down: searches simply miss the cache
        pass


def _index_version() -> int:
    try:
        return int(cache.get(INDEX_VERSION_KEY) or 0)
    except Exception:
        return 0


def _query_key(*parts) -> str:
    import hashlib
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ───────────────────────────── Celery tasks ─────────────────────────────────
@shared_task(
    name="document_search.index_file",
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(File.DoesNotExist,),
    retry_backoff=30,
    retry_backoff_max=1800,
    retry_jitter=True,
    max_retries=6,
)
def index_file(self, file_id: int, force: bool = False) -> dict:
    """
    Index one File into Milvus (vectors) and VectorChunk (cached embeddings).

    Safe to run any number of times: a per-file lock stops concurrent runs, Milvus
    rows for the file are replaced (never duplicated), and the file only counts as
    indexed once Milvus accepted the vectors, so a failure is retried instead of
    being remembered as done.
    """
    file = File.objects.select_related("user", "user__client").filter(pk=file_id).first()
    if file is None:
        LOGGER.warning("index_file: file %s does not exist (deleted or not committed).", file_id)
        return {"status": "error", "detail": "file-not-found"}

    if not force and file.vector_chunks.exists():
        return {"status": "skipped"}

    lock_key = f"lock:index_file:{file_id}"
    try:
        acquired = cache.add(lock_key, self.request.id or "1", timeout=LOCK_TTL)
    except Exception:
        acquired = True  # cache unavailable: indexing is idempotent anyway
    if not acquired:
        return {"status": "in-progress"}

    try:
        chunks, vectors = compute_chunks(file.filepath)
        _classify_document(file, chunks)
        if not chunks:
            LOGGER.warning("No extractable text for file %s (%s).", file_id, file.filename)
            milvus_store.delete_file_vectors(file.id)
            VectorChunk.objects.filter(file=file).delete()
            return {"status": "empty"}

        client_id = file.user.client_id if file.user and file.user.client_id else -1
        stored = milvus_store.replace_file_vectors(file.id, file.user_id, client_id, file.filename, chunks, vectors)

        with transaction.atomic():
            VectorChunk.objects.filter(file=file).delete()
            VectorChunk.objects.bulk_create(
                [VectorChunk(file=file, user_id=file.user_id, chunk_index=i, chunk_text=txt, embedding=vec)
                 for i, (txt, vec) in enumerate(zip(chunks, vectors))],
                batch_size=500,
            )
        _bump_index_version()
        LOGGER.info("Indexed file %s: %s chunks (%s stored in Milvus).", file_id, len(chunks), stored)
        return {"status": "ok", "chunks": len(chunks)}
    finally:
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def _classify_document(file, chunks) -> None:
    """Lightweight whole-document type label from one embedding (no File.save: avoids re-index signals)."""
    all_text = " ".join(chunks) if chunks else ""
    if not all_text.strip():
        label = guess_type_from_filename(file.filename) or "Unknown"
        File.objects.filter(pk=file.pk).update(document_type=label)
        return
    try:
        label = classify_document_content(_get_model(), all_text[:20000])
    except Exception as exc:
        LOGGER.warning("Classification failed for file %s: %s", file.pk, exc)
        label = guess_type_from_filename(file.filename) or "Unknown"
    File.objects.filter(pk=file.pk).update(document_type=label)


@shared_task(name="document_search.classify_file")
def classify_file(file_id: int, force: bool = False) -> dict:
    """
    Standalone document-type classification, decoupled from indexing.

    Runs for EVERY uploaded file regardless of file type, auto-index
    settings or duplicate-MD5 skips — those conditions previously left
    document_type NULL ("Unknown Type" on the dashboard).

    Order of attempts:
      1. Text extraction + embedding similarity (same model as index_file)
      2. OCR rasterization for image-only PDFs
      3. Filename keyword hints
      4. Literal "Unknown"
    """
    from document_search.utils import extract_text

    try:
        file = File.objects.select_related("user").get(pk=file_id)
    except File.DoesNotExist:
        LOGGER.error("classify_file: File %s does not exist.", file_id)
        return {"status": "error", "detail": "file-not-found"}

    if file.document_type and not force:
        return {"status": "skipped", "document_type": file.document_type}

    # 1️⃣ Direct text extraction
    try:
        text = extract_text(file.filepath)
    except Exception as exc:
        LOGGER.warning("classify_file: extraction error for %s: %s", file_id, exc)
        text = ""

    # 2️⃣ OCR fallback for scanned/image-only PDFs
    if not text.strip() and str(file.filepath).lower().endswith(".pdf"):
        text = _ocr_pdf_pages(file.filepath)

    if text.strip():
        try:
            embed_model = _get_model()
            label = classify_document_content(embed_model, text[:50000])
        except Exception as exc:
            LOGGER.error("classify_file: embedding failed for %s: %s", file_id, exc)
            label = guess_type_from_filename(file.filename) or "Unknown"
    else:
        # 3️⃣ Filename hints as last resort
        label = guess_type_from_filename(file.filename) or "Unknown"
        if label == "Unknown":
            LOGGER.info("classify_file: no extractable text or hints for file %s", file_id)

    # update(), not save(): a label change must not re-trigger indexing signals.
    File.objects.filter(pk=file.pk).update(document_type=label)
    LOGGER.info("→ classify_file: File %s classified as: %s", file_id, label)
    return {"status": "ok", "document_type": label}


@shared_task(name="document_search.bulk_reindex")
def bulk_reindex(batch_size: int = 1000) -> dict:
    """
    Queue indexing for all Files missing VectorChunks.
    Uses chunked iteration to scale to billions of files.
    """
    total = File.objects.count()
    LOGGER.info("Starting bulk reindex of %s total files ...", total)

    unindexed_qs = File.objects.filter(vector_chunks__isnull=True).order_by("id").distinct()
    unindexed_count = unindexed_qs.count()
    LOGGER.info("Found %s unindexed files.", unindexed_count)

    queued = 0
    ids_batch = []
    for f_id in unindexed_qs.values_list("id", flat=True).iterator(chunk_size=batch_size):
        ids_batch.append(f_id)
        if len(ids_batch) >= batch_size:
            _reindex_batch.delay(ids_batch)
            queued += len(ids_batch)
            ids_batch = []

    if ids_batch:
        _reindex_batch.delay(ids_batch)
        queued += len(ids_batch)

    LOGGER.info("Enqueued %s files for Milvus indexing.", queued)
    return {"queued": queued, "total": total, "unindexed": unindexed_count}


@shared_task(name="document_search.reindex_batch")
def _reindex_batch(file_ids: list[int]) -> dict:
    """Index a batch of file IDs into Milvus."""
    indexed = 0
    for fid in file_ids:
        try:
            index_file.apply(args=(fid,), kwargs={"force": False}, throw=True)
            indexed += 1
        except Exception as e:
            LOGGER.error("Failed to index file %s: %s", fid, e)
    return {"indexed": indexed, "batch_size": len(file_ids)}


@shared_task(name="document_search.exec_search")
def exec_search(user_id: int, query: str, file_id: int | None, top_k: int) -> list[dict]:
    cache_key = "search:v3:" + _query_key(_index_version(), user_id, file_id, top_k, query)
    cached = cache.get(cache_key)
    if cached:
        return cached

    # 1) Resolve access
    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return []

    from django.db.models import F
    accessible_ids = set(get_user_accessible_file_ids(user))

    # If caller passed file_id, tighten scope
    if file_id:
        accessible_ids &= {int(file_id)}
    if not accessible_ids:
        return []

    # 2) Embed query, 3) search only files the user may read (organisation-filtered)
    q_vec = embed_text(query)
    client_id_val = user.client_id if getattr(user, "client_id", None) else -1
    res = [milvus_store.search(q_vec, accessible_ids, client_id_val, top_k)]

    # 5) Dedup + shape
    seen = set()
    hits = []
    for hit in res[0]:
        fid = int(hit.entity.get("file_id"))
        ctext = hit.entity.get("chunk_text", "")
        ch = hash(ctext)
        if ch in seen:
            continue
        seen.add(ch)

        snippet = (ctext[:297] + "…") if len(ctext) > 300 else ctext
        snippet = snippet.replace("\n", "  \n")

        hits.append({
            "file_id": fid,
            "snippet_md": snippet,
            "score": float(hit.score),
            "preview": preview_for_file(fid),
        })
        if len(hits) >= top_k:
            break

    cache.set(cache_key, hits, timeout=60 * 60)  # key changes whenever new documents are indexed
    SearchQueryLog.objects.create(
        user_id=user_id,
        file_id=file_id if File.objects.filter(id=file_id).exists() else None,
        query_text=query,
        top_k=top_k,
        duration_ms=None,  # you can restore timing if desired
        result_count=len(hits),
        result_json=hits,
    )
    return hits


@shared_task(name="document_search.semantic_search_task")
def semantic_search_task(
    user_id: int,
    query: str,
    top_k: int,
    file_id: int | None,
    filters: dict | None,
    project_id: str | None = None,
    service_id: str | None = None,
    generate_report: bool = False
) -> dict:
    """
    Perform semantic search asynchronously.

    Args:
        user_id: The user performing the search
        query: The search query
        top_k: Number of results to return
        file_id: Optional file ID to restrict search
        filters: Optional filters (created_from, created_to, author, project_id, service_id)
        project_id: Project ID for report registration (optional)
        service_id: Service ID for report registration (optional)
        generate_report: Whether to generate and register an HTML report

    Returns:
        Dict with results and optional report_file info
    """
    start_time = time.time()
    try:
        filters = filters or {}
        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
        if not user:
            return {"results": [], "count": 0}

        # 1) Access scope
        accessible_ids = set(get_user_accessible_file_ids(user))
        if file_id:
            accessible_ids &= {int(file_id)}
        if not accessible_ids:
            return {"results": [], "count": 0}

        # 2) Embed, 3) search only files the user may read (organisation-filtered)
        embed_model = _get_model()
        qvec = embed_model.encode([query])[0]
        client_id_val = user.client_id if getattr(user, "client_id", None) else -1
        results = [milvus_store.search(qvec, accessible_ids, client_id_val, top_k)]

        vector_file_ids = {int(hit.entity.get("file_id")) for hit in results[0]}
        allowed_ids = vector_file_ids & accessible_ids
        if not allowed_ids:
            return {"results": [], "count": 0}

        # 5) Apply optional metadata filters in Django
        q = Q(id__in=allowed_ids)
        if filters.get("created_from"):
            q &= Q(created_at__gte=datetime.fromisoformat(filters["created_from"]))
        if filters.get("created_to"):
            q &= Q(created_at__lte=datetime.fromisoformat(filters["created_to"]))
        if filters.get("author"):
            q &= Q(metadata__author__icontains=filters["author"])
        if filters.get("project_id"):
            q &= Q(project_id=filters["project_id"])
        if filters.get("service_id"):
            q &= Q(service_id=filters["service_id"])

        files = File.objects.filter(q).prefetch_related("metadata")

        # best hit per file
        best = {}
        for hit in results[0]:
            fid = int(hit.entity.get("file_id"))
            if fid not in allowed_ids:
                continue
            score = float(hit.score)
            chunk = hit.entity.get("chunk_text", "")
            if (fid not in best) or (score > best[fid][1]):
                best[fid] = (chunk, score)

        out = []
        for f in files:
            md = f.metadata.first()
            chunk_text, score = best.get(f.id, ("", None))
            out.append({
                "file_id": f.id,
                "filename": f.filename,
                "file_size": f.file_size,
                "file_type": f.file_type,
                "document_type": getattr(f, "document_type", None),
                "created_at": str(f.created_at) if f.created_at else None,
                "author": getattr(md, "author", None) if md else None,
                "keywords": getattr(md, "keywords", None) if md else None,
                "chunk_text": chunk_text,
                "score": score,
            })

        execution_time = time.time() - start_time

        response = {
            "results": out,
            "count": len(out),
            "query": query,
            "execution_time_seconds": round(execution_time, 2)
        }

        # Generate and register report if requested
        if generate_report and project_id and service_id:
            try:
                # Create a Run instance for the report
                from django.contrib.contenttypes.models import ContentType
                run = Run.objects.create(
                    user=user,
                    project_id=project_id,
                    service_id=service_id,
                    status="completed",
                    result_json=response
                )

                report_info = generate_and_register_service_report(
                    service_name="Semantic Search",
                    service_id="ai-semantic-search",
                    vertical="AI Services",
                    response_data=response,
                    user=user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="semantic-search-results",
                    query=query,
                    execution_time_seconds=execution_time,
                    additional_metadata={
                        "top_k": top_k,
                        "file_id_filter": file_id,
                        "filters_applied": bool(filters)
                    }
                )
                response["report_file"] = report_info
                LOGGER.info(f"✅ Generated semantic search report: {report_info.get('filename')}")
            except Exception as report_error:
                LOGGER.warning(f"Failed to generate report: {report_error}")
                response["report_error"] = str(report_error)

        return response

    except Exception as e:
        LOGGER.error("Error in semantic search task: %s", str(e))
        return {"error": str(e), "results": [], "count": 0}


# ───────────── Optional alias for semantic clarity ─────────────
process_file_for_search = index_file



@shared_task(name="document_search.delete_file_vectors", acks_late=True, autoretry_for=(Exception,),
             retry_backoff=30, retry_backoff_max=1800, max_retries=6)
def delete_file_vectors_task(file_id: int) -> str:
    milvus_store.delete_file_vectors(file_id)
    _bump_index_version()
    return "deleted"


def find_unindexed(limit: int | None = None) -> dict:
    """
    Files missing from Milvus or Elasticsearch. A file counts as missing from
    Milvus when it has no cached chunks, or has chunks but no vectors in Milvus
    (the "worked yesterday, gone today" case after a failed or lost insert).
    """
    qs = File.objects.exclude(document_type="deliverable").order_by("-id")
    ids = list(qs.values_list("id", flat=True)[:limit] if limit else qs.values_list("id", flat=True))
    with_chunks = set(VectorChunk.objects.filter(file_id__in=ids).values_list("file_id", flat=True).distinct())
    in_milvus = milvus_store.indexed_file_ids(sorted(with_chunks)) if with_chunks else set()
    milvus_missing = [i for i in ids if i not in with_chunks or i not in in_milvus]

    es_missing = []
    try:
        from elasticsearch_dsl.connections import connections as es_connections
        es = es_connections.get_connection()
        for i in range(0, len(ids), 1000):
            part = [str(x) for x in ids[i: i + 1000]]
            found = {d["_id"] for d in es.mget(index="files", body={"ids": part}, _source=False)["docs"] if d.get("found")}
            es_missing += [int(x) for x in part if x not in found]
    except Exception as exc:
        LOGGER.warning("Elasticsearch check skipped: %s", exc)
        es_missing = None
    return {"checked": len(ids), "milvus_missing": milvus_missing, "es_missing": es_missing}


@shared_task(name="document_search.repair_search_indexes")
def repair_search_indexes(limit: int | None = None) -> dict:
    """Nightly self-healing: re-queue every file missing from either search index."""
    from file_elasticsearch.tasks import es_index_file

    report = find_unindexed(limit)
    for fid in report["milvus_missing"]:
        index_file.delay(fid, force=True)
    for fid in report["es_missing"] or []:
        es_index_file.delay(fid)
    summary = {"checked": report["checked"], "milvus_requeued": len(report["milvus_missing"]),
               "es_requeued": len(report["es_missing"] or []), "es_checked": report["es_missing"] is not None}
    LOGGER.info("Search index repair: %s", summary)
    return summary
