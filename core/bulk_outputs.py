"""
Bulk job deliverables.

What users get from a bulk job:
  * the real processed documents (translated / OCR'd / anonymised / redacted ...)
    produced by each service, collected per input file;
  * one Excel workbook: a row per document with status, output documents,
    the service's key results and any error, plus a summary sheet;
  * one ZIP with every output document, named after its source document.

Both consolidated files are saved into the job's output folder
(.batch-output/<Service>/) and are downloadable from the bulk API.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import zipfile
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.utils import timezone

from core.models import BulkJob, BulkJobFileResult, File, Run, Storage

logger = logging.getLogger(__name__)

# Output records each core service writes, mapped to the attributes holding paths.
_ARTIFACT_SOURCES = [
    ("document_ocr.models", "OCRFile", "original_file", ["ocr_filepath", "docx_path"]),
    ("document_translation.models", "TranslationFile", "original_file", ["translated_filepath"]),
    # Never the entity-mapping file: it links pseudonyms back to real identities.
    ("document_anonymizer.models", "Anonymize", "original_file", ["anonymized_filepath"]),
    ("document_anonymizer.models", "DeAnonymize", "file", ["unmasked_filepath"]),
]

_MAX_CELL = 32000  # Excel's per-cell limit is 32767 characters


# ---------------------------------------------------------------------------
# Collecting the real outputs for one input document
# ---------------------------------------------------------------------------

def _artifact_paths(input_file: File, since) -> List[str]:
    import importlib

    paths: List[str] = []
    for module, model_name, fk, attrs in _ARTIFACT_SOURCES:
        try:
            model = getattr(importlib.import_module(module), model_name)
        except Exception:
            continue
        qs = model.objects.filter(**{fk: input_file})
        if since is not None and hasattr(model, "created_at"):
            qs = qs.filter(created_at__gte=since)
        for obj in qs:
            for attr in attrs:
                p = getattr(obj, attr, None)
                if p and isinstance(p, str) and os.path.isfile(p):
                    paths.append(p)
    return paths


def collect_output_files(input_file: File, since) -> List[File]:
    """Output documents produced for `input_file` since `since`, as File records."""
    paths = set(_artifact_paths(input_file, since))
    outputs = list(
        File.objects.filter(user=input_file.user, filepath__in=paths).exclude(pk=input_file.pk)
    ) if paths else []

    if not outputs and since is not None:
        # Fallback: services register outputs named after the source document.
        stem = os.path.splitext(input_file.filename)[0]
        if len(stem) >= 3:
            outputs = list(
                File.objects.filter(
                    user=input_file.user,
                    created_at__gte=since,
                    filename__icontains=stem,
                )
                .exclude(pk=input_file.pk)
                .exclude(filepath__contains=".batch-output")
                .exclude(filename__icontains="mapping")
                .order_by("created_at")[:10]
            )
    return [f for f in outputs if f.filepath and os.path.isfile(f.filepath)]


# ---------------------------------------------------------------------------
# Turning a service's return value into spreadsheet columns
# ---------------------------------------------------------------------------

_SKIP_KEYS = {"task_id", "status", "file_id", "user_id", "run_id", "id", "success"}


def _humanise(key: str) -> str:
    key = re.sub(r"[_\-]+", " ", str(key)).strip()
    return key[:1].upper() + key[1:]


def summarise_value(value: Any, prefix: str = "", depth: int = 0) -> Dict[str, Any]:
    """Flatten a service result into readable columns (scalars and short lists only)."""
    out: Dict[str, Any] = {}
    if value is None:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in _SKIP_KEYS or str(k).lower().endswith("_path") or str(k).lower().endswith("filepath"):
                continue
            label = f"{prefix}{_humanise(k)}"
            if isinstance(v, (str, int, float, bool)) or v is None:
                out[label] = v
            elif isinstance(v, dict) and depth < 1:
                out.update(summarise_value(v, prefix=f"{label} › ", depth=depth + 1))
            elif isinstance(v, (list, tuple)):
                if all(isinstance(x, (str, int, float)) for x in v):
                    out[label] = "; ".join(str(x) for x in v)
                else:
                    out[f"{label} (count)"] = len(v)
    elif isinstance(value, (list, tuple)):
        out[f"{prefix}Items"] = len(value)
    elif isinstance(value, (str, int, float, bool)):
        out[f"{prefix}Result"] = value
    return out


# ---------------------------------------------------------------------------
# Registering consolidated deliverables
# ---------------------------------------------------------------------------

def _register_file(job: BulkJob, path: str, folder) -> Optional[File]:
    """Register a file on disk as a File owned by the job's user, linked into `folder`."""
    from document_operations.models import FileFolderLink

    with open(path, "rb") as fh:
        md5 = hashlib.md5(fh.read()).hexdigest()

    run = Run.objects.create(user=job.user, status="Completed")
    storage = Storage.objects.create(
        user=job.user,
        content_type=ContentType.objects.get_for_model(Run),
        object_id=run.run_id,
        upload_storage_location=path,
        output_storage_location=None,
    )
    try:
        f = File.objects.create(
            filename=os.path.basename(path),
            filepath=path,
            file_size=os.path.getsize(path),
            file_type=mimetypes.guess_type(path)[0] or os.path.splitext(path)[1].lstrip("."),
            extension=os.path.splitext(path)[1].lstrip(".")[:10],
            md5_hash=md5,
            status="Completed",
            run=run,
            user=job.user,
            project_id=job.project_id,
            service_id=job.service_id,
            storage=storage,
        )
    except IntegrityError:
        f = File.objects.filter(user=job.user, md5_hash=md5).first()
    if f and folder is not None:
        FileFolderLink.objects.get_or_create(file=f, folder=folder)
    return f


def _output_dir(job: BulkJob, first_input: Optional[File], service_name: str) -> str:
    from django.conf import settings

    if first_input and first_input.filepath:
        base = os.path.dirname(first_input.filepath)
    else:
        base = os.path.join(settings.MEDIA_ROOT, "bulk", str(job.user_id))
    safe_service = re.sub(r"[^\w\- ]+", "", service_name).strip() or "Bulk"
    path = os.path.join(base, ".batch-output", safe_service)
    os.makedirs(path, exist_ok=True)
    return path


def _unique_arcname(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    candidate, i = name, 2
    while candidate.lower() in used:
        candidate = f"{stem} ({i}){ext}"
        i += 1
    used.add(candidate.lower())
    return candidate


def build_deliverables(job: BulkJob, service_name: str, output_folder=None) -> Dict[str, Any]:
    """Create the Excel workbook and ZIP for a finished job. Returns their File ids."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    results = list(
        BulkJobFileResult.objects.filter(bulk_job=job).select_related("file").order_by("file__filename")
    )
    first_input = next((r.file for r in results if r.file), None)
    out_dir = _output_dir(job, first_input, service_name)
    stamp = timezone.localtime(job.completed_at or timezone.now()).strftime("%Y-%m-%d %H%M")
    base_name = f"{service_name} - {stamp}"

    # ---- Excel ------------------------------------------------------------
    field_order: List[str] = []
    rows = []
    for r in results:
        data = r.result_data or {}
        fields = data.get("fields") or {}
        for k in fields:
            if k not in field_order:
                field_order.append(k)
        rows.append((r, data, fields))

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    header = ["Document", "Status", "Output documents"] + field_order + ["Error"]
    ws.append(header)
    for r, data, fields in rows:
        outputs = ", ".join(o.get("filename", "") for o in data.get("outputs", []))
        values = [
            r.file.filename if r.file else f"File {r.file_id}",
            r.status,
            outputs or ("—" if r.status == "Completed" else ""),
        ]
        for k in field_order:
            v = fields.get(k)
            values.append(str(v)[:_MAX_CELL] if isinstance(v, str) else v)
        values.append((r.error_message or "")[:_MAX_CELL])
        ws.append(values)

    head_fill = PatternFill("solid", fgColor="1F1C19")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    for idx, title in enumerate(header, start=1):
        width = max([len(str(title))] + [len(str(c.value or "")) for c in ws[get_column_letter(idx)][1:200]])
        ws.column_dimensions[get_column_letter(idx)].width = min(max(12, width + 2), 60)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    summary = wb.create_sheet("Summary")
    total = len(results)
    done = sum(1 for r in results if r.status == "Completed")
    failed = sum(1 for r in results if r.status == "Failed")
    for k, v in [
        ("Service", service_name),
        ("Job", str(job.id)),
        ("Submitted", timezone.localtime(job.created_at).strftime("%Y-%m-%d %H:%M") if job.created_at else ""),
        ("Finished", timezone.localtime(job.completed_at).strftime("%Y-%m-%d %H:%M") if job.completed_at else ""),
        ("Documents", total),
        ("Completed", done),
        ("Failed", failed),
        ("Output documents", sum(len((r.result_data or {}).get("outputs", [])) for r in results)),
    ]:
        summary.append([k, v])
    for cell in summary["A"]:
        cell.font = Font(bold=True)
    summary.column_dimensions["A"].width = 22
    summary.column_dimensions["B"].width = 48

    xlsx_path = os.path.join(out_dir, f"{base_name} - results.xlsx")
    wb.save(xlsx_path)
    deliverables: Dict[str, Any] = {}
    xlsx_file = _register_file(job, xlsx_path, output_folder)
    if xlsx_file:
        deliverables["xlsx_file_id"] = xlsx_file.id

    # ---- ZIP of output documents ------------------------------------------
    output_ids = [o["file_id"] for r in results for o in (r.result_data or {}).get("outputs", []) if o.get("file_id")]
    if output_ids:
        files = {f.id: f for f in File.objects.filter(id__in=output_ids, user=job.user)}
        zip_path = os.path.join(out_dir, f"{base_name} - documents.zip")
        used: set = set()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for r in results:
                src = r.file.filename if r.file else f"file-{r.file_id}"
                for o in (r.result_data or {}).get("outputs", []):
                    f = files.get(o.get("file_id"))
                    if not f or not os.path.isfile(f.filepath):
                        continue
                    # Group by source document so outputs are easy to match up.
                    arc = _unique_arcname(f"{os.path.splitext(src)[0]}/{f.filename}", used)
                    zf.write(f.filepath, arcname=arc)
            zf.writestr("results.xlsx", open(xlsx_path, "rb").read())
        zip_file = _register_file(job, zip_path, output_folder)
        if zip_file:
            deliverables["zip_file_id"] = zip_file.id

    deliverables["generated_at"] = datetime.utcnow().isoformat() + "Z"
    return deliverables


def describe_outputs(files: Iterable[File]) -> List[Dict[str, Any]]:
    return [{"file_id": f.id, "filename": f.filename} for f in files]
