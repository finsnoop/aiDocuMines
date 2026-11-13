"""
Recompute stored PII risk scores with the corrected, PII-only formula.

Usage:
    python manage.py recompute_anonymization_risk
    python manage.py recompute_anonymization_risk --include-inactive
"""

from django.core.management.base import BaseCommand

from document_anonymizer.models import Anonymize
from document_anonymizer.utils import get_shared_anonymization_service


class Command(BaseCommand):
    help = (
        "Recompute risk_score / risk_level / breakdown for anonymization records "
        "using the corrected PII-only scoring policy (non-PII entities and "
        "cross-engine duplicates are excluded)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also recompute records whose is_active=False.",
        )

    def handle(self, *args, **options):
        qs = Anonymize.objects.select_related("original_file").order_by("created_at")
        if not options["include_inactive"]:
            qs = qs.filter(is_active=True)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No matching Anonymize records."))
            return

        self.stdout.write(f"Recomputing risk scores for {total} record(s)...")
        service = get_shared_anonymization_service()

        updated = failed = 0
        for index, record in enumerate(qs.iterator(), start=1):
            try:
                file_entry = record.original_file
                original_text = file_entry.content
                if not original_text:
                    try:
                        with open(file_entry.filepath, "r", encoding="utf-8") as f:
                            original_text = f.read()
                    except Exception:
                        original_text = ""

                result = service.compute_risk_score(
                    original_text or "",
                    record.presidio_masking_map or {},
                    record.spacy_masking_map or {},
                )

                record.risk_score = result["risk_score"]
                record.risk_level = result["risk_level"]
                record.risk_breakdown = result["breakdown"]
                record.risk_contributions = result.get("contributions")
                record.save(update_fields=[
                    "risk_score", "risk_level", "risk_breakdown",
                    "risk_contributions", "updated_at",
                ])
                updated += 1

                if index % 25 == 0 or index == total:
                    self.stdout.write(f"  {index}/{total} processed...")

            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"Failed record {record.id} (file={record.original_file_id}): {exc}"
                    )
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. updated={updated} failed={failed} total={total}"
        ))
