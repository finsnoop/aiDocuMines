"""
Seed a LOCAL DEMO environment with one account per role in two separate
organisations, so the UI, roles and tenant isolation can be tried out.

    ALLOW_DEMO_SEED=true python manage.py seed_demo --password "<demo password>" --out /tmp/demo_users.json

Refuses to run unless ALLOW_DEMO_SEED=true, so it can never be run against
production by accident. Idempotent: re-running updates passwords and roles.
The --out file (email, password, OAuth client id/secret) is used to create the
matching accounts in the Node service.
"""
import json
import os
import zlib

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from oauth2_provider.generators import generate_client_secret
from oauth2_provider.models import Application

from custom_authentication.models import Client

DEMO_ACCOUNTS = [
    # email, contact name, organisation, roles, is_staff, is_superuser
    ("superadmin@aidocumines.demo", "Sam Platform", "aiDocuMines (platform)", ["SuperAdmin"], True, True),
    ("admin@harperlowe.demo", "Alex Admin", "Harper & Lowe LLP", ["Admin"], True, False),
    ("manager@harperlowe.demo", "Morgan Manager", "Harper & Lowe LLP", ["Manager"], False, False),
    ("associate@harperlowe.demo", "Avery Associate", "Harper & Lowe LLP", ["Client"], False, False),
    ("compliance@harperlowe.demo", "Casey Compliance", "Harper & Lowe LLP", ["ComplianceOfficer"], False, False),
    ("auditor@harperlowe.demo", "Jordan Auditor", "Harper & Lowe LLP", ["Auditor"], False, False),
    ("developer@harperlowe.demo", "Devon Developer", "Harper & Lowe LLP", ["Developer"], False, False),
    ("guest@harperlowe.demo", "Glen Guest", "Harper & Lowe LLP", ["Guest"], False, False),
    ("admin@northwind.demo", "Nora Admin", "Northwind Legal", ["Admin"], True, False),
    ("associate@northwind.demo", "Noel Associate", "Northwind Legal", ["Client"], False, False),
]

# Sample matters: (owner email, project_id, service_id, folder, [(filename, text)])
DEMO_DOCUMENTS = [
    ("associate@harperlowe.demo", "Project Atlas", "due-diligence", "Commercial contracts", [
        ("Supply Agreement - Northwind.pdf",
         "SUPPLY AGREEMENT. 14.2 Change of Control. A Change of Control of the Customer shall be deemed an "
         "assignment and requires the prior written consent of the Supplier. 18. Assignment. Neither party may "
         "assign this Agreement without consent."),
        ("Master Services Agreement.pdf",
         "MASTER SERVICES AGREEMENT. 22.1 Either party may terminate on a Change of Control of the other party. "
         "23. Assignment permitted on 30 days written notice."),
        ("Distribution Agreement EU.pdf",
         "DISTRIBUTION AGREEMENT. 9.4 The Distributor may assign this Agreement to an Affiliate."),
    ]),
    ("associate@harperlowe.demo", "Project Atlas", "due-diligence", "Employment", [
        ("CEO Service Agreement.pdf",
         "SERVICE AGREEMENT. The Executive is entitled to twelve months notice. Good leaver provisions apply on a "
         "Change of Control."),
    ]),
    ("associate@northwind.demo", "Meridian v Halcyon", "litigation", "Pleadings", [
        ("Claim Form.pdf", "CLAIM FORM. The Claimant claims damages for breach of contract. CONFIDENTIAL TO NORTHWIND."),
        ("Witness Statement - J Park.pdf",
         "WITNESS STATEMENT of J Park. I, J Park, of 12 Harbour Road, make this statement. CONFIDENTIAL TO NORTHWIND."),
    ]),
]


def _pdf_bytes(text: str) -> bytes:
    """A small, valid single-page PDF containing `text` (no external libraries)."""
    words, lines, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > 90:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 11 Tf 56 780 Td 14 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    stream = zlib.compress(body.encode("latin-1", "replace"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


class Command(BaseCommand):
    help = "Seed a local demo environment (accounts for every role, two organisations, sample matters)."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True, help="Password for every demo account")
        parser.add_argument("--out", default="", help="Write accounts + OAuth credentials to this JSON file")
        parser.add_argument("--no-documents", action="store_true")

    def handle(self, *args, **opts):
        if os.environ.get("ALLOW_DEMO_SEED", "").lower() != "true":
            raise CommandError("Refusing to seed: set ALLOW_DEMO_SEED=true (local demo environments only).")

        from custom_authentication.rbac import seed_permissions_and_roles
        seed_permissions_and_roles()

        User = get_user_model()
        password = opts["password"]
        exported = []

        with transaction.atomic():
            for email, name, org_name, roles, staff, superuser in DEMO_ACCOUNTS:
                org, _ = Client.objects.get_or_create(name=org_name)
                user = User.objects.filter(email=email).first()
                if user is None:
                    user = User.objects.create_user(email=email, client=org, password=password)
                user.client = org
                user.username = email.split("@")[0].replace(".", "_") + "_" + org_name.split()[0].lower()
                user.contact_name = name
                user.is_staff = staff
                user.is_superuser = superuser
                user.is_active = True
                user.set_password(password)
                user.save()
                user.groups.set([Group.objects.get_or_create(name=r)[0] for r in roles])

                # Fresh secret each run; captured before the library hashes it on save.
                raw_secret = generate_client_secret()
                app = Application.objects.filter(user=user, name="aiDocuMines demo").first()
                if app is None:
                    app = Application.objects.create(
                        user=user, name="aiDocuMines demo",
                        client_type=Application.CLIENT_CONFIDENTIAL,
                        authorization_grant_type=Application.GRANT_PASSWORD,
                        client_secret=raw_secret,
                    )
                else:
                    app.client_secret = raw_secret
                    app.save()
                exported.append({
                    "email": email, "password": password, "name": name, "organisation": org_name,
                    "roles": roles, "client_id": app.client_id, "client_secret": raw_secret,
                })
                self.stdout.write(f"  {email:34} {org_name:24} {', '.join(roles)}")

        if not opts["no_documents"]:
            self._seed_documents()

        if opts["out"]:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                json.dump(exported, fh, indent=2)
            os.chmod(opts["out"], 0o600)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(exported)} demo accounts."))

    def _seed_documents(self):
        import hashlib
        from core.models import File, Run
        from document_operations.utils import register_file_folder_link

        User = get_user_model()
        base = os.path.join(settings.MEDIA_ROOT, "demo")
        count = 0
        for email, project, service, folder, docs in DEMO_DOCUMENTS:
            owner = User.objects.get(email=email)
            run = Run.objects.create(user=owner, status="Completed")
            for filename, text in docs:
                path = os.path.join(base, str(owner.id), project, service, folder, filename)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = _pdf_bytes(text)
                with open(path, "wb") as fh:
                    fh.write(data)
                md5 = hashlib.md5(data).hexdigest()
                f, created = File.objects.get_or_create(
                    user=owner, md5_hash=md5,
                    defaults=dict(filename=filename, filepath=path, file_size=len(data), file_type="application/pdf",
                                  extension="pdf", status="Completed", run=run, project_id=project,
                                  service_id=service, content=text),
                )
                if created:
                    register_file_folder_link(f)
                    count += 1
        self.stdout.write(f"  sample documents created: {count}")
