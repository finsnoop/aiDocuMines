"""
Copy private-equity post-close obligations into organisation-wide obligations (Contracts, Obligations), so they get
reminders, owners and recurrence like every other obligation. The private-equity records are left untouched.
Safe to run again: an obligation already imported (same organisation, title and due date) is skipped.

    docker exec aidocumines_web python manage.py import_pe_obligations --dry-run
    docker exec aidocumines_web python manage.py import_pe_obligations [--organisation <id>]
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from contracts.models import Obligation

RECURRENCE = {"monthly": "monthly", "quarterly": "quarterly", "annually": "yearly", "annual": "yearly"}
STATUS = {"completed": "done", "approved": "done", "waived": "waived"}


class Command(BaseCommand):
    help = "Copy private-equity post-close obligations into organisation-wide obligations."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--organisation", type=int)

    def handle(self, *args, **opts):
        from private_equity.models import PostCloseObligation
        qs = PostCloseObligation.objects.exclude(due_date__isnull=True, next_due_date__isnull=True).select_related("assigned_to")
        if opts["organisation"]:
            qs = qs.filter(client_id=opts["organisation"])
        created = skipped = 0
        with transaction.atomic():
            for p in qs.iterator():
                due = p.next_due_date or p.due_date
                title = (p.obligation_name or "Post-close obligation")[:255]
                if Obligation.objects.filter(client_id=p.client_id, title=title, due_date=due).exists():
                    skipped += 1
                    continue
                owner = p.assigned_to if p.assigned_to and p.assigned_to.client_id == p.client_id else None
                if not opts["dry_run"]:
                    Obligation.objects.create(
                        client_id=p.client_id, title=title, kind="obligation", due_date=due,
                        description=(p.description or "")[:5000] + (f"\n\nSource clause: {p.source_clause[:2000]}" if p.source_clause else ""),
                        recurrence=RECURRENCE.get(p.frequency, "none"), owner=owner,
                        status=STATUS.get(p.status, "open"), created_by=p.created_by)
                created += 1
            if opts["dry_run"]:
                transaction.set_rollback(True)
        verb = "Would import" if opts["dry_run"] else "Imported"
        self.stdout.write(f"{verb} {created} obligation(s); {skipped} already present.")
