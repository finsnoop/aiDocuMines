"""
Reprocess previously anonymized files IN PLACE with the corrected PII policy.

For every Anonymize record this regenerates all artifacts into the SAME file
paths already stored on the record and updates the SAME database row, so:
  - Anonymize primary keys are preserved
  - original_file / run foreign keys are preserved
  - core.File rows registered for the artifacts stay accurate (same paths)
  - masking maps are refreshed (non-PII noise removed going forward)

Usage:
    python manage.py reprocess_anonymizations                # active records
    python manage.py reprocess_anonymizations --backup       # keep .bak copies
    python manage.py reprocess_anonymizations --include-inactive
    python manage.py reprocess_anonymizations --record-id <uuid>
    python manage.py reprocess_anonymizations --verify-only  # linkage audit
"""

import json
import os
import shutil

from django.core.management.base import BaseCommand
from django.db import transaction

from document_anonymizer.models import Anonymize
from document_anonymizer.utils import (
    AnonymizationService,
    generate_anonymized_html,
    get_shared_anonymization_service,
)


class Command(BaseCommand):
    help = (
        "Re-run PII detection/risk scoring for already-anonymized documents, "
        "rewriting artifacts at their stored paths and refreshing the same "
        "Anonymize rows (IDs, foreign keys and registered files untouched)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also reprocess records whose is_active=False.",
        )
        parser.add_argument(
            "--backup",
            action="store_true",
            help="Copy each existing artifact to '<path>.bak' before overwriting.",
        )
        parser.add_argument(
            "--record-id",
            type=str,
            default=None,
            help="Reprocess a single Anonymize record by ID.",
        )
        parser.add_argument(
            "--verify-only",
            action="store_true",
            help="Only audit DB<->disk linkage without modifying anything.",
        )

    # ------------------------------------------------------------------ utils

    def _write(self, path, data, backup):
        """Write text or JSON to path; optional .bak safety copy first."""
        if path is None:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if backup and os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        mode = "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write(data)

    def _resolve_original_text(self, file_entry, service):
        original = getattr(file_entry, "content", None)
        if not original:
            try:
                extracted, _, _ = service.extract_text_from_file(file_entry.filepath)
                original = extracted or ""
            except Exception:
                original = ""
        return original or ""

    def _reprocess_record(self, record, service, backup):
        """Regenerate artifacts in place; returns True when content changed."""
        file_entry = record.original_file

        structured_text, _, elements_json = service.extract_structured_text(file_entry.filepath)
        if not structured_text:
            raw_text, _, _ = service.extract_text_from_file(file_entry.filepath)
            if not raw_text:
                raise FileNotFoundError(f"No extractable content at {file_entry.filepath}")
            elements_json = [{"element_id": "raw-0", "type": "Text", "text": raw_text}]
            structured_text = raw_text

        updated_blocks = []
        combined_map, presidio_map, spacy_map = {}, {}, {}
        for block in elements_json:
            masked, combined, pm, sm = service.anonymize_text(block.get("text", ""))
            block["text"] = masked
            updated_blocks.append(block)
            combined_map.update(combined)
            presidio_map.update(pm)
            spacy_map.update(sm)

        final_masked_doc = "\n\n".join(b["text"] for b in updated_blocks)

        self._write(record.anonymized_filepath, final_masked_doc, backup)
        self._write(
            record.entity_mapping_filepath,
            json.dumps(combined_map, indent=4),
            backup,
        )
        if record.anonymized_html_filepath:
            try:
                self._write(
                    record.anonymized_html_filepath,
                    generate_anonymized_html(final_masked_doc, combined_map),
                    backup,
                )
            except Exception:
                pass

        if record.file_type == "structured":
            if record.anonymized_structured_filepath:
                self._write(
                    record.anonymized_structured_filepath,
                    json.dumps(updated_blocks, indent=2),
                    backup,
                )
            if record.anonymized_structured_txt_filepath:
                self._write(record.anonymized_structured_txt_filepath, structured_text, backup)
            if record.anonymized_structured_html_filepath:
                try:
                    self._write(
                        record.anonymized_structured_html_filepath,
                        generate_anonymized_html(structured_text, {}),
                        backup,
                    )
                except Exception:
                    pass

        risk_result = service.compute_risk_score(
            self._resolve_original_text(file_entry, service),
            presidio_map,
            spacy_map,
        )

        with transaction.atomic():
            record.presidio_masking_map = presidio_map
            record.spacy_masking_map = spacy_map
            record.risk_score = risk_result["risk_score"]
            record.risk_level = result_level = risk_result["risk_level"]
            record.risk_breakdown = risk_result["breakdown"]
            record.risk_contributions = risk_result.get("contributions")
            record.status = "Completed"
            record.save(update_fields=[
                "presidio_masking_map", "spacy_masking_map",
                "risk_score", "risk_level", "risk_breakdown",
                "risk_contributions", "status", "updated_at",
            ])
        return result_level

    def _verify(self, queryset):
        problems = []
        ok = 0
        for record in queryset.iterator():
            issues = []
            for field in ("anonymized_filepath", "entity_mapping_filepath"):
                p = getattr(record, field)
                if not p or not os.path.exists(p):
                    issues.append(f"{field} missing on disk: {p}")
            if record.entity_mapping_filepath and os.path.exists(record.entity_mapping_filepath):
                try:
                    with open(record.entity_mapping_filepath, "r", encoding="utf-8") as f:
                        json.load(f)
                except Exception as exc:
                    issues.append(f"entity mapping unreadable: {exc}")
            if not record.original_file_id:
                issues.append("original_file link missing")
            if issues:
                problems.append((str(record.id), issues))
            else:
                ok += 1
        return ok, problems

    # ---------------------------------------------------------------- handler

    def handle(self, *args, **options):
        qs = Anonymize.objects.select_related("original_file").order_by("created_at")
        if options["record_id"]:
            qs = qs.filter(id=options["record_id"])
        elif not options["include_inactive"]:
            qs = qs.filter(is_active=True)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No matching Anonymize records."))
            return

        if options["verify_only"]:
            ok, problems = self._verify(qs)
            self.stdout.write(self.style.SUCCESS(f"{ok}/{total} records fully linked."))
            for rid, issues in problems:
                self.stdout.write(self.style.ERROR(f"  {rid}:"))
                for issue in issues:
                    self.stdout.write(f"     - {issue}")
            return

        self.stdout.write(f"Reprocessing {total} record(s)...")
        service = get_shared_anonymization_service()

        done = failed = 0
        levels = {"High": 0, "Medium": 0, "Low": 0, "Ok": 0}
        for index, record in enumerate(qs.iterator(), start=1):
            label = f"{record.original_file.filename} [{record.file_type}]"
            try:
                level = self._reprocess_record(record, service, options["backup"])
                levels[level] = levels.get(level, 0) + 1
                done += 1
                self.stdout.write(
                    f"  [{index}/{total}] OK  {label} -> risk {level} "
                    f"({record.risk_score})"
                )
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"  [{index}/{total}] FAILED {label}: {exc}")
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. reprocessed={done} failed={failed} | "
            f"risk distribution: {levels}"
        ))
