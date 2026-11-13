import os
import logging
from celery import shared_task
from django.db import transaction
from django.shortcuts import get_object_or_404
from core.models import File
from document_versioning.models import DocumentVersion, VersionDiff
from document_versioning.utils import (
    compute_file_hash,
    compute_file_size,
    compute_mime_type,
    create_version_snapshot,
    generate_diff,
    generate_version_manifest,
    enforce_retention_policy,
    get_next_version_number,
)
from core.utils import register_generated_file, get_logical_path


logger = logging.getLogger(__name__)


@shared_task
def create_version_task(file_id, run_id):
    logger.info(f"Creating version snapshot for file_id={file_id}, run_id={run_id}")

    file_instance = get_object_or_404(File, id=file_id)

    if not os.path.exists(file_instance.filepath):
        logger.error(f"File not found: {file_instance.filepath}")
        return {"error": "File not found", "file_id": file_id}

    version_number = get_next_version_number(file_instance)
    created_by = str(file_instance.user) if file_instance.user else "system"

    snapshot_path = create_version_snapshot(
        file_instance, file_instance.filepath, version_number, created_by
    )
    file_hash = compute_file_hash(snapshot_path, algorithm='md5')
    file_size = compute_file_size(snapshot_path)
    mime_type = compute_mime_type(file_instance.filename)
    checksum_sha256 = compute_file_hash(snapshot_path, algorithm='sha256')

    with transaction.atomic():
        version = DocumentVersion.objects.create(
            file=file_instance,
            version_number=version_number,
            filepath=snapshot_path,
            file_hash=file_hash,
            file_size=file_size,
            mime_type=mime_type,
            checksum_sha256=checksum_sha256,
            created_by=created_by,
        )

        registered = register_generated_file(
            file_path=snapshot_path,
            user=file_instance.user,
            run=file_instance.run,
            project_id=file_instance.project_id,
            service_id=file_instance.service_id,
            folder_name=os.path.join("versions", f"v{version_number}"),
        )

        previous_version = DocumentVersion.objects.filter(
            file=file_instance,
            version_number=version_number - 1,
        ).first()

        if previous_version and os.path.exists(previous_version.filepath):
            try:
                diff_text, diff_html, additions, deletions, similarity_ratio = generate_diff(
                    previous_version.filepath, snapshot_path
                )
                if diff_text:
                    VersionDiff.objects.create(
                        file=file_instance,
                        from_version=previous_version,
                        to_version=version,
                        diff_content=diff_text,
                        diff_html=diff_html,
                        additions=additions,
                        deletions=deletions,
                        similarity_ratio=similarity_ratio,
                    )
                    logger.info(f"Diff stored between v{previous_version.version_number} and v{version_number}")
            except Exception as e:
                logger.error(f"Failed to generate diff: {e}")

        try:
            generate_version_manifest(version)
            logger.info(f"Manifest generated for version {version_number}")
        except Exception as e:
            logger.error(f"Failed to generate manifest: {e}")

        try:
            enforce_retention_policy(file_instance)
        except Exception as e:
            logger.error(f"Failed to enforce retention policy: {e}")

    logger.info(f"Version {version_number} created for file_id={file_id}")
    return {
        "file_id": file_id,
        "version_number": version_number,
        "version_id": str(version.id),
        "filepath": get_logical_path(snapshot_path),
        "file_hash": file_hash,
        "checksum_sha256": checksum_sha256,
        "file_size": file_size,
        "mime_type": mime_type,
        "registered_outputs": [{
            "filename": registered.filename,
            "file_id": registered.id,
            "path": get_logical_path(registered.filepath, registered.filename),
        }],
    }


@shared_task
def periodic_retention_enforcement():
    logger.info("Starting periodic retention enforcement")
    files = File.objects.all()
    processed = 0
    for file_instance in files:
        try:
            enforce_retention_policy(file_instance)
            processed += 1
        except Exception as e:
            logger.error(f"Retention enforcement failed for file {file_instance.id}: {e}")
    logger.info(f"Periodic retention enforcement completed for {processed} files")
    return {"processed_files": processed}
