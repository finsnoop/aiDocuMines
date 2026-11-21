"""Create the client demonstration account and load the Legal DMS/CLM test corpus through the platform's own
pipelines, so every feature can be shown and tested end to end.

    python manage.py load_demo_corpus --corpus /tmp/demo_corpus --as you@firm.com                  # check only
    python manage.py load_demo_corpus --corpus /tmp/demo_corpus --as you@firm.com --execute         # create

The corpus folder holds index.json (one row per document), matter_graph.json (matters, facts, deliberate
discrepancies) and the documents in sub-folders. Creates, in a new organisation (default "Datasqan Demo Law Firm"):
  * people with every working role, made exactly as Users & access makes them (sign-in registered, no invitation
    emails), plus a second organisation with one administrator to demonstrate tenant isolation;
  * the 22 matters (reference, practice area, facts) with teams;
  * every document, stored through core/ingest.py into its matter (text extraction, indexing and classification
    are queued as for any upload);
  * contract records for the agreements (counterparty, value, currency, date, stage, matter, document);
  * a commercial contracts playbook (incident notice <= 24 hours, liability caps, audit, DPA before transfer,
    sub-processors, governing law) applied to the procurement matter;
  * benchmarks: the SLA and the DPA frozen as benchmarks with measurable terms, in categories;
  * a legal hold on the IP litigation matter and an ethical wall on the acquisition matter.
Passwords are printed once at the end (give the demo password with --password to use one known password).
The check-only run verifies every document exists and matches its SHA-256 in index.json.
"""
import hashlib
import json
import os
import re
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

CONTRACT_KINDS = {"contract", "executed"}
TYPE_WORDS = [("Master Services", "MSA"), ("Non-Disclosure", "NDA"), ("Data Processing", "DPA"),
              ("Statement of Work", "SOW"), ("Service Level", "SLA"), ("Share Purchase", "SPA"),
              ("Shareholders", "Shareholders agreement"), ("Lease", "Lease"), ("Loan", "Loan agreement"),
              ("Licence", "Licence"), ("License", "Licence"), ("Settlement", "Settlement agreement"),
              ("Employment", "Employment"), ("Distribution", "Distribution agreement"), ("Guarantee", "Guarantee"),
              ("Services Agreement", "Services agreement"), ("Supply", "Supply agreement")]
MONEY = re.compile(r"\b([A-Z]{3})\s?([\d][\d,\s]*(?:\.\d+)?)\s*(million|m|bn|billion)?", re.I)

PEOPLE = [  # (local part, name, roles, matters: "all" | "half" | "none")
    ("nomvula.admin", "Nomvula Dlamini (Practice Administrator)", ["Admin"], "all"),
    ("james.partner", "James van der Merwe (Partner)", ["Manager"], "all"),
    ("aisha.associate", "Aisha Patel (Associate)", ["Client"], "all"),
    ("sipho.paralegal", "Sipho Ndlovu (Paralegal)", ["Client"], "half"),
    ("lerato.compliance", "Lerato Mokoena (Compliance Officer)", ["ComplianceOfficer"], "none"),
    ("thabo.auditor", "Thabo Khumalo (Auditor)", ["Auditor"], "none"),
    ("client.guest", "Client Guest (read only)", ["Guest"], "none"),
]

PLAYBOOK_POSITIONS = [
    ("Security incident notice", "high", "Supplier notifies the customer within 24 hours of confirming a security incident.",
     ["Within 48 hours, with an initial notice within 24 hours"], ["72 hours or longer", "No incident notice obligation"]),
    ("Limitation of liability", "high", "General cap of 12 months' charges; personal-data and cybersecurity claims capped "
     "at 24 months' charges; no cap for fraud or wilful misconduct.", ["Data and security claims capped at 18 months' charges"],
     ["Unlimited liability of the customer", "Data and security claims under the general 12-month cap"]),
    ("Audit rights", "medium", "Annual audit, quarterly audit evidence and reasonable on-site assessment.",
     ["Annual audit with remote evidence only"], ["No audit or inspection rights"]),
    ("Data processing agreement", "high", "A signed data processing agreement is in place before any personal data or "
     "analytics transfer.", [], ["Personal data transferred without a signed DPA"]),
    ("Sub-processors", "medium", "Prior written approval of any new sub-processor.",
     ["30 days' notice with a right to object"], ["New sub-processors without notice"]),
    ("Governing law and disputes", "low", "South African law; disputes in Johannesburg courts or arbitration.",
     ["English law for UK entities with London arbitration"], []),
]
BENCHMARKS = [  # (doc_id, name, kind, category, terms: (topic, requirement, comparator, target, unit, tolerance, severity))
    ("TEC-001-007", "Group SLA standard (service levels and credits)", "sla", "Service levels",
     [("Service availability", "At least 99.95% monthly availability", ">=", "99.95", "%", "0.05", "high"),
      ("Critical incident response", "Critical incidents answered within 1 hour", "<=", "1", "hours", "0", "high")]),
    ("TEC-001-008", "Data processing standard (POPIA / GDPR)", "policy", "Data protection",
     [("Security incident notice", "Notify within 24 hours of a confirmed security incident", "<=", "24", "hours", "0", "high"),
      ("Signed DPA before transfer", "A signed DPA is in place before personal data is transferred", "present", None, "", None, "high")]),
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def money(value):
    m = MONEY.search(value or "")
    if not m:
        return None, ""
    amount = Decimal(m.group(2).replace(",", "").replace(" ", ""))
    scale = (m.group(3) or "").lower()
    amount *= 1_000_000 if scale in ("million", "m") else 1_000_000_000 if scale in ("bn", "billion") else 1
    return amount.quantize(Decimal("0.01")), m.group(1).upper()


def contract_type(title):
    return next((t for words, t in TYPE_WORDS if words.lower() in title.lower()), "Agreement")


def matter_name(m):
    return re.sub(r"[/\\]+", "-", m["matter"]).strip()[:255]


class Command(BaseCommand):
    help = "Create the demo organisation and load the test corpus (check only unless --execute)."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", required=True, help="Folder with index.json, matter_graph.json and the documents.")
        parser.add_argument("--as", dest="actor", required=True, help="Platform administrator creating the account.")
        parser.add_argument("--org", default="Datasqan Demo Law Firm")
        parser.add_argument("--domain", default="demo.datasqan.com", help="Email domain for the demo people.")
        parser.add_argument("--password", default="", help="One known password for every demo person (else generated).")
        parser.add_argument("--execute", action="store_true")

    def handle(self, *args, corpus, actor, org, domain, password, execute, **opts):
        from core.access import is_platform_superuser
        from custom_authentication.models import Client, CustomUser
        index_path, graph_path = os.path.join(corpus, "index.json"), os.path.join(corpus, "matter_graph.json")
        if not (os.path.isfile(index_path) and os.path.isfile(graph_path)):
            raise CommandError("The corpus folder must contain index.json and matter_graph.json.")
        rows = json.load(open(index_path, encoding="utf-8"))
        graph = json.load(open(graph_path, encoding="utf-8"))
        problems = []
        for r in rows:
            path = os.path.join(corpus, r["filename"])
            if not os.path.isfile(path):
                problems.append(f"missing {r['filename']}")
            elif r.get("sha256") and sha256(path) != r["sha256"]:
                problems.append(f"checksum differs {r['filename']}")
        matters = {m["matter_id"]: m for m in graph}
        orphans = sorted({r["matter_id"] for r in rows} - set(matters))
        if orphans:
            problems.append(f"documents for unknown matters {orphans}")
        boss = CustomUser.objects.filter(email__iexact=actor, is_active=True).first()
        if boss is None or not is_platform_superuser(boss):
            problems.append(f"{actor} is not an active platform administrator")
        if Client.objects.filter(name=org).exists():
            problems.append(f"organisation {org!r} already exists (remove it with reset_platform first)")
        taken = CustomUser.objects.filter(email__iendswith="@" + domain).count()
        if taken:
            problems.append(f"{taken} accounts already use @{domain}")
        contracts = [r for r in rows if r.get("kind") in CONTRACT_KINDS]
        self.stdout.write(f"Corpus: {len(rows)} documents in {len(matters)} matters; {len(contracts)} contract records; "
                          f"{len(PEOPLE)} people + 1 isolation-test administrator; organisation {org!r}")
        for p in problems:
            self.stdout.write(self.style.ERROR(f"  problem: {p}"))
        if problems:
            raise CommandError("Fix the problems above; nothing was created.")
        if not execute:
            self.stdout.write(self.style.SUCCESS("Check passed: every document is present and matches its checksum. "
                                                 "Add --execute to create the demo account."))
            return
        report = self.create(boss, corpus, rows, matters, org, domain, password)
        self.stdout.write(json.dumps({k: v for k, v in report.items() if k != "people"}, indent=2, default=str))
        self.stdout.write("\nSign-in details (shown once; store them in your password manager):")
        for email, name, roles, pw, ready in report["people"]:
            self.stdout.write(f"  {email:<42} {pw:<20} {','.join(roles):<18} {name}{'' if ready else '  [sign-in NOT registered]'}")

    # ── creation ─────────────────────────────────────────────────────────────────────────────
    def create(self, boss, corpus, rows, matters, org, domain, password):
        from contracts.models import (Benchmark, BenchmarkCategory, BenchmarkTerm, Contract, KnowledgeLink, Playbook,
                                      PlaybookPosition)
        from core.ingest import MatterBatch
        from core.models import EthicalWall, File, LegalHold, Matter
        from custom_authentication.models import Client, OrganisationPolicy
        from custom_authentication.views_admin import provision_person
        from document_versioning.utils import get_next_version_number
        report = {"people": []}

        firm = Client.objects.create(name=org)
        OrganisationPolicy.objects.update_or_create(client=firm, defaults={
            "assistant_enabled": True, "watermark_downloads": True,
            "watermark_text": "Confidential · {email} · {date}"})
        people = {}
        for local, name, roles, _ in PEOPLE:
            email = f"{local}@{domain}"
            user, pw, ready, _ = provision_person(boss, email, name, roles, firm, password=password or None, send_invite=False)
            people[local] = user
            report["people"].append((email, name, roles, pw, ready))
        other = Client.objects.create(name="Isolation Test Firm")
        email = f"tenant.admin@isolation.{domain}"
        user, pw, ready, _ = provision_person(boss, email, "Isolation tester (other organisation)", ["Admin"], other,
                                              password=password or None, send_invite=False)
        report["people"].append((email, user.contact_name, ["Admin"], pw, ready))

        owner = people["james.partner"]
        matter_objs, ordered = {}, sorted(matters.values(), key=lambda m: m["matter_id"])
        for i, m in enumerate(ordered):
            facts = "\n".join(f"- {f}" for f in m.get("facts", []))
            issues = "\n".join(f"- {d}" for d in m.get("known_test_discrepancies", []))
            mat = Matter.objects.create(
                client=firm, name=matter_name(m), reference=m["matter_id"], practice_area=m.get("vertical", "")[:100],
                description=(f"Parties: {', '.join(m.get('parties', []))}. Value: {m.get('value', '')}. "
                             f"Jurisdiction: {m.get('jurisdiction', '')}.\n\nKey facts:\n{facts}\n\nKnown issues to test:\n{issues}")[:20000],
                created_by=owner)
            team = [people[local] for local, _, _, scope in PEOPLE
                    if scope == "all" or (scope == "half" and i < len(ordered) // 2)]
            mat.members.add(*team)
            matter_objs[m["matter_id"]] = mat
        report["matters"] = len(matter_objs)

        files, by_matter = {}, {}
        for r in rows:
            by_matter.setdefault(r["matter_id"], []).append(r)
        for matter_id, docs in by_matter.items():
            batch = MatterBatch(owner, matter_objs[matter_id], source="demo-corpus")
            try:
                for r in docs:
                    path = os.path.join(corpus, r["filename"])
                    with open(path, "rb") as fh:
                        entry = batch.add_stream(os.path.basename(path), iter(lambda: fh.read(1 << 20), b""))
                    if entry.get("file_id"):
                        files[r["doc_id"]] = File.objects.get(pk=entry["file_id"])
                    else:
                        report.setdefault("not_stored", []).append(f"{r['doc_id']}: {entry.get('message')}")
            finally:
                batch.finish()
        report["documents"] = len(files)

        made = 0
        for r in rows:
            if r.get("kind") not in CONTRACT_KINDS or r["doc_id"] not in files:
                continue
            amount, currency = money(r.get("value", ""))
            status = (r.get("status") or "").lower()
            stage = "active" if any(w in status for w in ("executed", "signed", "final", "active")) else \
                "review" if "review" in status or "negotiat" in status else "draft"
            Contract.objects.create(
                client=firm, matter=matter_objs[r["matter_id"]], title=f"{r['title']} ({r['matter_id']})"[:255],
                counterparty=(r.get("party_2") or "")[:255], contract_type=contract_type(r["title"]),
                description=(r.get("focus") or "")[:5000], stage=stage, value=amount, currency=currency,
                start_date=r.get("record_date") or None, document=files[r["doc_id"]], owner=owner, requested_by=owner)
            made += 1
        report["contracts"] = made

        tec = matter_objs.get("TEC-001")
        playbook = Playbook.objects.create(
            client=firm, name="Commercial & SaaS Contracts Playbook", contract_type="MSA", category="legal-contracts",
            tags=["commercial", "saas", "data-protection"], document_type="Contract", use_case="Deviation Analysis",
            description="Negotiation positions for supplier, SaaS and outsourcing agreements.",
            content="Positions for incident notice, liability caps, audit, data processing, sub-processors and governing law. "
                    "Check every draft and executed agreement against these before approval.",
            source_file=files.get("TEC-001-014"), created_by=boss, updated_by=boss)
        for order, (topic, severity, standard, fallbacks, red_lines) in enumerate(PLAYBOOK_POSITIONS, start=1):
            PlaybookPosition.objects.create(playbook=playbook, topic=topic, severity=severity, standard=standard,
                                            fallbacks=fallbacks, red_lines=red_lines, order=order)
        if tec:
            KnowledgeLink.objects.create(client=firm, playbook=playbook, matter=tec, created_by=boss)

        batch = MatterBatch(owner, self.knowledge_matter(firm, owner), source="benchmark")
        made_benchmarks = []
        try:
            for doc_id, name, kind, category, terms in BENCHMARKS:
                source = files.get(doc_id)
                if source is None:
                    continue
                copy, digest = batch.add_snapshot(source)
                bucket, _ = BenchmarkCategory.objects.get_or_create(client=firm, name=category, defaults={"created_by": boss})
                b = Benchmark.objects.create(
                    client=firm, name=name, kind=kind, file=copy, bucket=bucket, created_by=boss,
                    source_file=source, source_version=get_next_version_number(source),
                    source_project_id=source.project_id, source_service_id=source.service_id,
                    snapshot_at=timezone.now(), snapshot_sha256=digest)
                for order, (topic, requirement, comparator, target, unit, tolerance, severity) in enumerate(terms, start=1):
                    BenchmarkTerm.objects.create(benchmark=b, topic=topic, requirement=requirement, comparator=comparator,
                                                 target_value=Decimal(target) if target else None, unit=unit,
                                                 tolerance=Decimal(tolerance) if tolerance else None, severity=severity, order=order)
                playbook.benchmarks.add(b)
                if tec:
                    KnowledgeLink.objects.create(client=firm, benchmark=b, matter=tec, created_by=boss)
                made_benchmarks.append(name)
        finally:
            batch.finish()
        report["playbook"] = playbook.name
        report["benchmarks"] = made_benchmarks

        with transaction.atomic():
            ip = matter_objs.get("IP-003")
            if ip:
                LegalHold.objects.create(client=firm, matter=ip, name="Aurora v Bluewave litigation hold",
                                         reason="Litigation reasonably anticipated; preserve every document in the matter.",
                                         created_by=boss)
            ma = matter_objs.get("MA-002")
            if ma:
                wall = EthicalWall.objects.create(client=firm, matter=ma, created_by=boss,
                                                  reason="Conflict: the paralegal previously advised the seller.")
                wall.excluded.add(people["sipho.paralegal"])
        report["legal_hold"] = "IP-003 Aurora v Bluewave" if ip else None
        report["ethical_wall"] = "MA-002 excludes sipho.paralegal" if ma else None
        report["password_protected_pdf"] = "FIN-007-009 (test password in the corpus README: sandbox-test-2026)"
        report["processing"] = "Text extraction, OCR, indexing and classification are queued; allow a few minutes."
        return report

    @staticmethod
    def knowledge_matter(firm, owner):
        from core.models import Matter
        matter, _ = Matter.objects.get_or_create(client=firm, name="Knowledge library",
                                                 defaults={"created_by": owner, "practice_area": "Knowledge",
                                                           "description": "Playbook and benchmark documents."})
        matter.members.add(owner)
        return matter
