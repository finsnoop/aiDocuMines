import os
import hashlib
import difflib
import shutil
import json
import logging
import mimetypes
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from document_versioning.models import DocumentVersion, VersionManifest, VersionArchiveRecord, VersionRetentionPolicy


logger = logging.getLogger(__name__)


def compute_file_hash(filepath, algorithm='sha256'):
    if algorithm == 'sha256':
        hasher = hashlib.sha256()
    else:
        hasher = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_file_size(filepath):
    return os.path.getsize(filepath)


def compute_mime_type(filename):
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or 'application/octet-stream'


def create_version_snapshot(file_instance, filepath, version_number, created_by):
    base_dir = os.path.dirname(file_instance.filepath)
    versions_dir = os.path.join(base_dir, "versions")
    os.makedirs(versions_dir, exist_ok=True)
    ext = os.path.splitext(file_instance.filename)[1] if hasattr(file_instance, 'filename') and file_instance.filename else ""
    snapshot_filename = f"v{version_number}_{file_instance.id}{ext}"
    snapshot_path = os.path.join(versions_dir, snapshot_filename)
    shutil.copy2(filepath, snapshot_path)
    logger.info(f"Snapshot created: {snapshot_path}")
    return snapshot_path


def generate_diff(file1_path, file2_path):
    try:
        with open(file1_path, "r", errors="replace") as f1:
            lines1 = f1.readlines()
        with open(file2_path, "r", errors="replace") as f2:
            lines2 = f2.readlines()
    except Exception as e:
        logger.error(f"Failed to read files for diff: {e}")
        return "", "", 0, 0, None

    diff_lines = list(
        difflib.unified_diff(
            lines1, lines2,
            fromfile=os.path.basename(file1_path),
            tofile=os.path.basename(file2_path),
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    sm = difflib.SequenceMatcher(None, lines1, lines2)
    similarity_ratio = round(sm.ratio(), 4)

    additions = 0
    deletions = 0
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    diff_html = generate_word_level_diff_html(diff_lines)
    return diff_text, diff_html, additions, deletions, similarity_ratio


def generate_word_level_diff_html(diff_lines):
    html_lines = []
    for line in diff_lines:
        if line.startswith("+"):
            html_lines.append(f'<div class="diff-added" style="background-color: #ccffcc;">{line}</div>')
        elif line.startswith("-"):
            html_lines.append(f'<div class="diff-removed" style="background-color: #ffcccc;">{line}</div>')
        elif line.startswith("@@"):
            html_lines.append(f'<div class="diff-header" style="background-color: #e6e6e6; font-weight: bold;">{line}</div>')
        else:
            html_lines.append(f"<div class=\"diff-context\">{line}</div>")
    return "\n".join(html_lines)


def get_next_version_number(file_instance):
    latest = DocumentVersion.objects.filter(file=file_instance).order_by("-version_number").first()
    if latest:
        return latest.version_number + 1
    return 1


def enforce_retention_policy(file_instance):

    from core.holds import is_held
    if is_held(file_instance):
        return None  # versions of a document under legal hold are preserved as they are
    policy = VersionRetentionPolicy.objects.filter(project_id=file_instance.project_id).first()
    if not policy:
        logger.info(f"No retention policy found for project {file_instance.project_id}")
        return

    versions = DocumentVersion.objects.filter(file=file_instance).order_by("-version_number")
    total = versions.count()

    cutoff_date = timezone.now() - timedelta(days=policy.retention_days)
    archive_cutoff = timezone.now() - timedelta(days=policy.archive_after_days)

    for version in versions:
        if version.is_locked:
            continue
        if version.created_at < cutoff_date:
            if version.is_archived:
                _delete_version_file(version)
            else:
                _archive_or_delete_version(version)
        elif version.created_at < archive_cutoff and not version.is_archived:
            _archive_or_delete_version(version)

    if total > policy.max_versions:
        to_remove = versions[policy.max_versions:]
        for version in to_remove:
            if version.is_locked:
                continue
            if version.is_archived:
                _delete_version_file(version)
            else:
                _archive_or_delete_version(version)


def _archive_or_delete_version(version):
    try:
        archive = perform_archive(version)
        if archive:
            version.is_archived = True
            version.save(update_fields=['is_archived'])
            if os.path.exists(version.filepath):
                os.remove(version.filepath)
    except Exception as e:
        logger.error(f"Failed to archive version {version.id}: {e}")
        _delete_version_file(version)


def _delete_version_file(version):
    if os.path.exists(version.filepath):
        try:
            os.remove(version.filepath)
        except Exception as e:
            logger.error(f"Failed to delete file for version {version.id}: {e}")
    version.is_archived = True
    version.save(update_fields=['is_archived'])


def generate_version_manifest(version):
    previous_version = DocumentVersion.objects.filter(
        file=version.file,
        version_number=version.version_number - 1
    ).first()

    manifest_data = {
        "version_id": str(version.id),
        "file_id": str(version.file.id),
        "filename": version.file.filename,
        "version_number": version.version_number,
        "file_hash": version.file_hash,
        "checksum_sha256": version.checksum_sha256,
        "file_size": version.file_size,
        "mime_type": version.mime_type,
        "created_by": version.created_by,
        "timestamp": version.created_at.isoformat(),
        "previous_version_hash": previous_version.file_hash if previous_version else None,
        "change_summary": version.change_summary,
        "signature": None,
    }

    manifest = VersionManifest.objects.create(
        version=version,
        manifest_data=manifest_data,
        checksum_algorithm='SHA256',
    )
    return manifest


def perform_archive(version, archive_format='PDF/A'):
    if not os.path.exists(version.filepath):
        logger.warning(f"Version file not found for archiving: {version.filepath}")
        return None

    base_dir = os.path.dirname(os.path.dirname(version.filepath))
    archive_dir = os.path.join(base_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    ext = os.path.splitext(version.file.filename)[1] if version.file.filename else ""
    archive_filename = f"v{version.version_number}_{version.file.id}_archive{ext}"
    archive_path = os.path.join(archive_dir, archive_filename)

    shutil.copy2(version.filepath, archive_path)
    size_bytes = os.path.getsize(archive_path)

    archive_record = VersionArchiveRecord.objects.create(
        version=version,
        archived_path=archive_path,
        archive_format=archive_format,
        size_bytes=size_bytes,
    )
    logger.info(f"Version {version.id} archived to {archive_path}")
    return archive_record


def restore_from_archive(archive_record, restored_by):
    if not os.path.exists(archive_record.archived_path):
        raise FileNotFoundError(f"Archived file not found: {archive_record.archived_path}")

    version = archive_record.version
    restored_path = version.filepath.replace('/versions/', '/restored/')

    restore_dir = os.path.dirname(restored_path)
    os.makedirs(restore_dir, exist_ok=True)

    shutil.copy2(archive_record.archived_path, restored_path)

    version.filepath = restored_path
    version.is_archived = False
    version.save(update_fields=['filepath', 'is_archived'])

    archive_record.restored_at = timezone.now()
    archive_record.restored_by = restored_by
    archive_record.save(update_fields=['restored_at', 'restored_by'])

    return restored_path
