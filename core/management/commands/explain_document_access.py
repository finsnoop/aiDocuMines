"""Explain why a person sees a document in My documents (read-only; changes nothing).

    python manage.py explain_document_access --viewer someone@firm.com [--filename part] [--limit 20]

For the viewer: platform superuser? organisation administrator? which organisation?
For each matching document they can see: owner and owner's organisation, the access rule that makes it visible
(organisation admin / own document / shared / matter team; platform superusers see no other organisation's documents), and its folder links
(active, archived, none). Documents with no folder link at all are listed as "legacy, unlinked": the table shows
those even though they are in no folder, so a document whose links were removed but whose record remains
reappears there.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.access import (_matter_member_files_q, _shared_file_ids_q, is_platform_superuser, is_tenant_admin,
                         tenant_id)


class Command(BaseCommand):
    help = "Explain why a person can see documents in the documents table (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--viewer", required=True, help="Email of the person whose view is explained.")
        parser.add_argument("--filename", default="", help="Only documents whose name contains this text.")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, viewer, filename, limit, **opts):
        from core.models import File
        from document_operations.document_query import accessible_document_query
        from document_operations.models import FileFolderLink
        user = get_user_model().objects.filter(email__iexact=viewer).select_related("client").first()
        if user is None:
            raise CommandError("No user with that email.")
        tid = tenant_id(user)
        superuser, org_admin = is_platform_superuser(user), is_tenant_admin(user)
        self.stdout.write(f"Viewer {user.email}: organisation={getattr(user.client, 'name', None)!r} (id {tid}), "
                          f"platform superuser={superuser}, organisation admin={org_admin}, "
                          f"roles={sorted(user.groups.values_list('name', flat=True))}")
        rows = accessible_document_query(user, {"search": filename} if filename else {}).select_related("user", "user__client")
        total = rows.count()
        self.stdout.write(f"Documents in their table{' matching ' + repr(filename) if filename else ''}: {total}")
        shared = set(File.objects.filter(_shared_file_ids_q(user, "view")).values_list("pk", flat=True)) if tid else set()
        team = set(File.objects.filter(_matter_member_files_q(user, "view")).values_list("pk", flat=True)) if tid else set()
        by_rule = {}
        for f in rows[:max(1, limit)]:
            owner_org = getattr(f.user, "client_id", None)
            if f.user_id == user.pk:
                rule = "own document"
            elif f.pk in shared:
                rule = "shared with them"
            elif f.pk in team:
                rule = "matter team member"
            elif org_admin and owner_org == tid:
                rule = "organisation admin (sees every document of the organisation)"
            else:
                rule = "UNEXPLAINED - report this"
            links = list(FileFolderLink.objects.filter(file=f).values_list("is_trashed", "folder__is_trashed"))
            state = ("legacy, unlinked (no folder link)" if not links else
                     f"{sum(1 for a, b in links if not a and not b)} active / {len(links)} links")
            by_rule[rule] = by_rule.get(rule, 0) + 1
            self.stdout.write(f"- #{f.pk} {f.filename!r} owner={getattr(f.user, 'email', None)} "
                              f"org={getattr(getattr(f.user, 'client', None), 'name', None)!r} | {rule} | {state}")
        unlinked = rows.exclude(pk__in=FileFolderLink.objects.values("file_id")).count()
        self.stdout.write(f"Of all {total}: {unlinked} have no folder link at all (legacy, unlinked).")
        for rule, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  shown above because {rule}: {n}")
