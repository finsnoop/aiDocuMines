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
    # ── Fictional portfolio for the executive / operations dashboards (dates as of November 2026) ──
    ("associate@harperlowe.demo", "Portfolio review", "contracts", "Commercial", [
        ("SAMPLE Cloud Hosting Agreement - Nimbus.pdf",
         "SAMPLE CLOUD HOSTING AGREEMENT between Harper & Lowe LLP and Nimbus Hosting (Pty) Ltd. "
         "This Agreement commences on 1 December 2024 and expires on 5 December 2026 unless renewed. "
         "The total contract value is ZAR 2 400 000 payable monthly. Either party must give written notice of "
         "non-renewal no later than 30 November 2026. The Supplier's aggregate liability shall not exceed "
         "ZAR 500 000. This Agreement renews automatically for twelve months unless notice is given."),
        ("SAMPLE Office Lease - Riverside Towers.pdf",
         "SAMPLE LEASE AGREEMENT for Floors 7 and 8, Riverside Towers. The lease commences on 1 February 2022 "
         "and expires on 31 January 2027. The annual rental is ZAR 3 960 000 escalating at 7% per annum. "
         "Early termination by the Tenant attracts a termination fee of ZAR 1 200 000."),
        ("SAMPLE Software Licence - LexiCore.pdf",
         "SAMPLE SOFTWARE LICENCE AGREEMENT between Harper & Lowe LLP and LexiCore Inc. Effective date: "
         "1 July 2023. This licence expired on 30 June 2026. The licence fee is USD 180 000 per year. "
         "The Licensor may terminate for convenience on 60 days notice. Liability is unlimited for breach of "
         "confidentiality."),
        ("SAMPLE Master Services Agreement - Atlas Consulting.pdf",
         "SAMPLE MASTER SERVICES AGREEMENT between Harper & Lowe LLP and Atlas Consulting LLC. This Agreement "
         "is effective from 15 March 2025 and ends on 14 March 2028. The contract value is USD 750 000. "
         "Each party's liability shall not exceed USD 750 000. A change of control of the Supplier allows the "
         "Customer to terminate immediately."),
        ("SAMPLE Master Services Agreement - Atlas Consulting (v2).pdf",
         "SAMPLE MASTER SERVICES AGREEMENT between Harper & Lowe LLP and Atlas Consulting LLC. This Agreement "
         "is effective from 15 March 2025 and ends on 14 March 2028. The contract value is USD 790 000. "
         "Each party's liability shall not exceed USD 750 000. A change of control of the Supplier allows the "
         "Customer to terminate immediately."),
    ]),
    ("associate@harperlowe.demo", "Portfolio review", "finance", "Invoices and payroll", [
        ("SAMPLE Tax Invoice INV-2026-0412.pdf",
         "SAMPLE TAX INVOICE INV-2026-0412. Invoice date: 2 October 2026. Bill to: Harper & Lowe LLP. "
         "Professional services subtotal ZAR 86 956.52. VAT (15%) amount ZAR 13 043.48. Total ZAR 100 000.00. "
         "Amount paid ZAR 40 000.00. Balance due ZAR 60 000.00 by 1 November 2026. Interest of 2% per month "
         "applies to overdue amounts."),
        ("SAMPLE Tax Invoice INV-2026-0388.pdf",
         "SAMPLE TAX INVOICE INV-2026-0388. Invoice date: 5 September 2026. Bill to: Harper & Lowe LLP. "
         "VAT (15%) amount ZAR 6 521.74. Total ZAR 50 000.00. Amount paid ZAR 50 000.00 on 30 September 2026."),
        ("SAMPLE Payslip October 2026 - T Mokoena.pdf",
         "SAMPLE PAYSLIP. Employee: T Mokoena. Pay period: October 2026. Gross pay ZAR 64 500.00. "
         "PAYE ZAR 16 240.00. Deductions ZAR 3 870.00. Net pay ZAR 44 390.00."),
    ]),
    ("manager@harperlowe.demo", "Global mobility - Rivera family", "immigration", "Applicant documents", [
        ("SAMPLE Passport - A Rivera.pdf",
         "SAMPLE PASSPORT. Passport No. X0000000. Surname: RIVERA. Given names: ANA. Nationality: Fictional "
         "Republic. Date of birth: 4 April 1990. Date of issue: 10 May 2019. Date of expiry: 9 May 2029."),
        ("SAMPLE Critical Skills Visa - A Rivera.pdf",
         "SAMPLE VISA. Visa type: Critical Skills Work Visa. Number of entries: Multiple. Valid from 1 March 2024 "
         "valid until 28 February 2027. Holder: Ana Rivera."),
        ("SAMPLE Curriculum Vitae - A Rivera.pdf",
         "SAMPLE CURRICULUM VITAE. Ana Rivera. Professional experience: Senior data engineer, 2016 to present. "
         "Education: BSc Computer Science. Skills: Python, cloud platforms, data governance."),
        ("SAMPLE Motivation Letter - A Rivera.pdf",
         "SAMPLE MOTIVATION LETTER. Dear Sir or Madam, I am writing to apply for a critical skills work visa "
         "renewal. I would welcome the opportunity to continue contributing my expertise."),
        ("SAMPLE Proof of Address - A Rivera.pdf",
         "SAMPLE PROOF OF RESIDENCE. Municipal account for Ana Rivera, residential address 14 Fictional Lane, "
         "Cape Town. Statement date: 1 October 2026."),
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
