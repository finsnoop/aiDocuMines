"""
Read-only: explain why documents are still visible after their folders were archived and deleted.

    docker exec aidocumines_web python manage.py document_leftovers --email you@example.com
    docker exec aidocumines_web python manage.py document_leftovers            # every organisation

Each visible document is put in one bucket:
  unlinked      no folder link at all (older uploads); folder actions never reach it
  copy          another document row points at the same stored file (the same upload added to
                another matter); deleting one copy keeps the others and the bytes
  hidden        only hidden from this user (someone else's document); Archive "delete" un-hid it
  archived_up   its own folder is active but a parent folder is archived
  linked        in an active folder (normal)
Nothing is changed.
"""
from collections import Counter, defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count


class Command(BaseCommand):
    help = "Read-only report of documents that survive folder deletion, and why."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Report what this user sees (default: every document).")
        parser.add_argument("--limit", type=int, default=25, help="Example rows to print per bucket.")

    def handle(self, *args, **opts):
        from core.models import File
        from document_operations.models import FileFolderLink, Folder, UserFileHide, UserFolderHide

        user = None
        files = File.objects.select_related("user")
        if opts["email"]:
            user = get_user_model().objects.filter(email__iexact=opts["email"]).first()
            if user is None:
                raise CommandError("No user with that email.")
            from core.access import accessible_files
            files = accessible_files(user).select_related("user")

        files = list(files.only("id", "filename", "filepath", "project_id", "service_id", "user__email", "origin_file_id"))
        ids = [f.id for f in files]
        links = defaultdict(list)
        for link in FileFolderLink.objects.filter(file_id__in=ids).select_related("folder"):
            links[link.file_id].append(link)
        shared_paths = set(File.objects.exclude(filepath__isnull=True).exclude(filepath="")
                           .values("filepath").annotate(n=Count("id")).filter(n__gt=1).values_list("filepath", flat=True))
        hidden_links, hidden_folders = set(), set()
        if user is not None:
            hidden_links = set(UserFileHide.objects.filter(user=user).values_list("file_link_id", flat=True))
            hidden_folders = set(UserFolderHide.objects.filter(user=user).values_list("folder_id", flat=True))
        parents = dict(Folder.objects.values_list("id", "parent_id"))
        trashed = set(Folder.objects.filter(is_trashed=True).values_list("id", flat=True))

        def ancestors(fid):
            seen = set()
            while fid and fid not in seen:
                seen.add(fid)
                yield fid
                fid = parents.get(fid)

        buckets = defaultdict(list)
        for f in files:
            mine = links.get(f.id, [])
            active = [l for l in mine if not l.is_trashed and l.folder_id not in trashed]
            if not mine:
                bucket = "unlinked"
            elif user is not None and active and all(
                    l.id in hidden_links or any(a in hidden_folders for a in ancestors(l.folder_id)) for l in active):
                bucket = "hidden"
            elif active and all(any(a in trashed for a in ancestors(l.folder_id)) for l in active):
                bucket = "archived_up"
            elif not active:
                continue  # archived: correctly not shown
            else:
                bucket = "linked"
            if bucket in ("linked", "unlinked") and (f.filepath in shared_paths or f.origin_file_id):
                bucket = "copy" if bucket == "linked" else "unlinked"
            buckets[bucket].append(f)

        scope = f"visible to {user.email}" if user else "all organisations"
        self.stdout.write(f"Documents {scope}: {sum(len(v) for v in buckets.values())}")
        for name in ("unlinked", "copy", "hidden", "archived_up", "linked"):
            rows = buckets.get(name, [])
            self.stdout.write(f"\n{name:<12} {len(rows)}")
            if not rows:
                continue
            by_matter = Counter(r.project_id or "-" for r in rows)
            self.stdout.write("  by matter: " + ", ".join(f"{m} ({n})" for m, n in by_matter.most_common(10)))
            owners = Counter(r.user.email if r.user else "-" for r in rows)
            self.stdout.write("  owners:    " + ", ".join(f"{o} ({n})" for o, n in owners.most_common(5)))
            for r in rows[: opts["limit"]]:
                self.stdout.write(f"    #{r.id:<7} {r.filename[:50]:<50} matter={r.project_id} service={r.service_id}")
