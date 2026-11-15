"""
Seed a scale-test dataset: N organisations x M users with folders, documents, matters and shares.

    ALLOW_DEMO_SEED=true python manage.py seed_scale --tenants 5 --users 500 --files-per-user 8 \
        --out /tmp/scale_tokens.json

Every organisation's data carries a unique marker (ZQALPHA, ZQBRAVO, ...) in names and text, so an
isolation sweep can detect any response that contains another organisation's data. Each user gets an
access token (written to --out) so load tests authenticate exactly like real users (bearer token +
X-Client-ID of the organisation's application). Never run against a production database.
"""
import json
import os
import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

MARKERS = ["ZQALPHA", "ZQBRAVO", "ZQCHARLIE", "ZQDELTA", "ZQECHO", "ZQFOXTROT", "ZQGOLF", "ZQHOTEL"]
# per 500 users; scaled for other sizes
ROLE_MIX = [("Admin", 2), ("Manager", 25), ("Auditor", 5), ("ComplianceOfficer", 3)]   # the rest: Client
DOC_KINDS = [
    ("Master Services Agreement", "contracts", "This Master Services Agreement is entered into by {org}. "
     "Total contract value ZAR 1 200 000. Expires on 30 June 2027. Liability capped at ZAR 500 000."),
    ("Tax Invoice", "finance", "Tax invoice issued to {org}. Amount due ZAR 45 000 including VAT of ZAR 5 869.57."),
    ("Employment Contract", "employment", "Employment contract between {org} and an employee. Notice period 30 days."),
    ("Non-Disclosure Agreement", "contracts", "Mutual non-disclosure agreement for {org}. Term of two years."),
    ("Board Resolution", "governance", "Resolution of the board of {org} approving the annual budget."),
    ("Lease Agreement", "property", "Office lease for {org}. Monthly rental ZAR 85 000, escalation 8% per year."),
    ("Policy Document", "compliance", "Data protection policy of {org} under POPIA and GDPR."),
    ("Due Diligence Report", "due-diligence", "Due diligence findings for {org}: three material risks identified."),
]


class Command(BaseCommand):
    help = "Seed organisations x users with documents, matters and shares for isolation and load tests."

    def add_arguments(self, parser):
        parser.add_argument("--tenants", type=int, default=5)
        parser.add_argument("--users", type=int, default=500, help="users per organisation")
        parser.add_argument("--files-per-user", type=int, default=8)
        parser.add_argument("--matters", type=int, default=25, help="matters per organisation")
        parser.add_argument("--out", default="", help="write users, roles and access tokens to this JSON file")

    def handle(self, *args, **o):
        if os.environ.get("ALLOW_DEMO_SEED", "").lower() != "true":
            raise CommandError("Refusing to seed: set ALLOW_DEMO_SEED=true (test environments only).")
        if o["tenants"] > len(MARKERS):
            raise CommandError(f"At most {len(MARKERS)} organisations.")
        from custom_authentication.rbac import seed_permissions_and_roles
        seed_permissions_and_roles()
        rng = random.Random(42)
        out = []
        for t in range(o["tenants"]):
            out += self._tenant(MARKERS[t], o, rng)
        if o["out"]:
            with open(o["out"], "w", encoding="utf-8") as fh:
                json.dump(out, fh)
            os.chmod(o["out"], 0o600)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(out)} users in {o['tenants']} organisations."))

    @transaction.atomic
    def _tenant(self, marker, o, rng):
        from oauth2_provider.models import AccessToken, Application
        from custom_authentication.models import Client
        from core.models import File, Matter, Run
        from document_operations.models import FileFolderLink, Folder

        User = get_user_model()
        org, _ = Client.objects.get_or_create(name=f"Scale Org {marker}", defaults={"industry": "Legal"})
        if User.objects.filter(client=org).exists():
            raise CommandError(f"{org.name} already has users; use a fresh database.")
        password = make_password(f"Scale-{marker}-not-a-real-password")
        n = o["users"]
        roles = []
        for role, per500 in ROLE_MIX:
            roles += [role] * max(1, round(per500 * n / 500))
        roles += ["Client"] * (n - len(roles))

        users = User.objects.bulk_create([
            User(email=f"u{i:04d}.{marker.lower()}@scale.test", username=f"u{i:04d}_{marker.lower()}",
                 contact_name=f"{marker} Person {i}", client=org, password=password, is_active=True,
                 client_secret_hashed=f"scale-{marker.lower()}-{i}-{rng.getrandbits(64):016x}",   # unique column
                 is_staff=roles[i] == "Admin")
            for i in range(n)])
        groups = {g: Group.objects.get_or_create(name=g)[0] for g in set(roles)}
        User.groups.through.objects.bulk_create([
            User.groups.through(**{f"{User._meta.model_name}_id": u.id, "group_id": groups[roles[i]].id})
            for i, u in enumerate(users)])

        app = Application.objects.create(
            user=users[0], name=f"Scale {marker}", client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_PASSWORD)
        expires = timezone.now() + timedelta(days=14)
        tokens = AccessToken.objects.bulk_create([
            AccessToken(user=u, application=app, token=f"scale-{marker.lower()}-{u.id}-{rng.getrandbits(96):024x}",
                        expires=expires, scope="read write")
            for u in users])

        matters = Matter.objects.bulk_create([
            Matter(client=org, name=f"{marker} Matter {m:02d}", reference=f"{marker}-{m:03d}",
                   practice_area="General", created_by=users[0]) for m in range(o["matters"])])
        Matter.members.through.objects.bulk_create([
            Matter.members.through(matter_id=mt.id, **{f"{User._meta.model_name}_id": u.id})
            for mt in matters for u in rng.sample(users, min(20, n))])

        base = os.path.join(settings.MEDIA_ROOT, "scale", marker)
        files, links_for = [], []
        runs = Run.objects.bulk_create([Run(user=u, status="Completed") for u in users])
        for i, u in enumerate(users):
            matter = matters[i % len(matters)]
            for k in range(o["files_per_user"]):
                title, service, text = DOC_KINDS[k % len(DOC_KINDS)]
                name = f"{marker} {title} {i:04d}-{k}.pdf"
                files.append(File(user=u, filename=name, filepath=os.path.join(base, str(u.id), name),
                                  file_size=4096 + k, file_type="application/pdf", extension="pdf",
                                  status="Completed", run=runs[i], project_id=matter.name, service_id=service,
                                  content=f"{marker} " + text.format(org=f"Scale Org {marker}"),
                                  md5_hash=f"{marker}{u.id:08d}{k:04d}".ljust(32, "0")[:32]))
        files = File.objects.bulk_create(files, batch_size=2000)

        # One folder tree per user and service: <matter>/<service>
        folders = {}
        for f in files:
            key = (f.user_id, f.project_id, f.service_id)
            if key not in folders:
                root = folders.get((f.user_id, f.project_id))
                if root is None:
                    root = Folder.objects.create(name=f.project_id, user_id=f.user_id,
                                                 project_id=f.project_id, service_id=f.service_id)
                    folders[(f.user_id, f.project_id)] = root
                folders[key] = Folder.objects.create(name=f"{marker} {f.service_id}", parent=root, user_id=f.user_id,
                                                     project_id=f.project_id, service_id=f.service_id)
        links = FileFolderLink.objects.bulk_create(
            [FileFolderLink(file=f, folder=folders[(f.user_id, f.project_id, f.service_id)]) for f in files],
            batch_size=2000)

        # Each user shares their first document with three colleagues (same organisation only).
        share_rows = []
        for i, u in enumerate(users):
            link = links[i * o["files_per_user"]]
            link.is_shared = True
            for colleague in rng.sample(users, min(4, n)):
                if colleague.id != u.id:
                    share_rows.append(FileFolderLink.shared_with.through(
                        filefolderlink_id=link.id, **{f"{User._meta.model_name}_id": colleague.id}))
        FileFolderLink.objects.bulk_update([links[i * o["files_per_user"]] for i in range(n)], ["is_shared"])
        FileFolderLink.shared_with.through.objects.bulk_create(share_rows, ignore_conflicts=True)

        self.stdout.write(f"  {org.name}: {n} users, {len(files)} documents, {len(matters)} matters, "
                          f"{len(share_rows)} shares")
        return [{"organisation": org.name, "org_id": org.id, "marker": marker, "user_id": u.id, "email": u.email,
                 "role": roles[i], "token": tokens[i].token, "client_id": app.client_id}
                for i, u in enumerate(users)]
