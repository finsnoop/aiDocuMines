"""
Contract lifecycle (CLM): contracts with stages, intake requests, approval routing and obligations.

Stage flow (contracts/lifecycle.py enforces it):
    request -> draft -> review -> approval -> signature -> active -> renewal -> active | expired
    any open stage -> terminated
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

STAGES = [
    ("request", "Requested"), ("draft", "Drafting"), ("review", "In review"), ("approval", "Awaiting approval"),
    ("signature", "Out for signature"), ("active", "Active"), ("renewal", "Up for renewal"),
    ("expired", "Expired"), ("terminated", "Terminated"),
]
CLOSED_STAGES = {"expired", "terminated"}


class Contract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="contracts")
    matter = models.ForeignKey("core.Matter", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    title = models.CharField(max_length=255)
    counterparty = models.CharField(max_length=255, blank=True, default="")
    contract_type = models.CharField(max_length=60, blank=True, default="")      # e.g. NDA, MSA, Employment, Lease
    description = models.TextField(blank=True, default="")
    stage = models.CharField(max_length=12, choices=STAGES, default="request", db_index=True)
    value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    notice_days = models.PositiveIntegerField(null=True, blank=True)
    auto_renew = models.BooleanField(default=False)
    renewal_term_months = models.PositiveIntegerField(null=True, blank=True)
    needed_by = models.DateField(null=True, blank=True)
    document = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_contracts")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="requested_contracts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["client", "stage"]), models.Index(fields=["client", "end_date"])]

    def __str__(self):
        return self.title


class ContractEvent(models.Model):
    """Append-only history of a contract: stage changes, approvals, edits."""
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30)                 # stage, approval, edit, renewal, created
    from_stage = models.CharField(max_length=12, blank=True, default="")
    to_stage = models.CharField(max_length=12, blank=True, default="")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Contract history cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Contract history cannot be deleted.")


class ApprovalRule(models.Model):
    """Who must approve a contract before signature: by type and minimum value, a named person or a role."""
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="contract_approval_rules")
    name = models.CharField(max_length=255)
    contract_type = models.CharField(max_length=60, blank=True, default="")   # blank = every type
    min_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    approver_role = models.CharField(max_length=50, blank=True, default="")
    order = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]

    def applies_to(self, contract) -> bool:
        if self.contract_type and self.contract_type.lower() != (contract.contract_type or "").lower():
            return False
        if self.min_value is not None and (contract.value or Decimal(0)) < self.min_value:
            return False
        return True


class ContractApproval(models.Model):
    STATUSES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"), ("void", "Superseded")]
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="approvals")
    rule = models.ForeignKey(ApprovalRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="contract_approvals")
    approver_role = models.CharField(max_length=50, blank=True, default="")
    order = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, choices=STATUSES, default="pending")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order", "id"]


class Obligation(models.Model):
    """Something someone must do by a date: from a contract (renewal notice, expiry, payment) or added by hand."""
    KINDS = [("obligation", "Obligation"), ("notice", "Notice"), ("renewal", "Renewal decision"),
             ("expiry", "Expiry"), ("payment", "Payment")]
    STATUSES = [("open", "Open"), ("done", "Done"), ("waived", "Waived")]
    RECURRENCES = [("none", "Once"), ("monthly", "Monthly"), ("quarterly", "Quarterly"), ("yearly", "Yearly")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="obligations")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="obligations")
    matter = models.ForeignKey("core.Matter", null=True, blank=True, on_delete=models.SET_NULL, related_name="obligations")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=12, choices=KINDS, default="obligation")
    due_date = models.DateField(db_index=True)
    recurrence = models.CharField(max_length=10, choices=RECURRENCES, default="none")
    reminder_days = models.PositiveIntegerField(default=14)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="obligations")
    status = models.CharField(max_length=8, choices=STATUSES, default="open", db_index=True)
    automatic = models.BooleanField(default=False)       # created from the contract's dates
    last_reminded_on = models.DateField(null=True, blank=True)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date", "id"]
        indexes = [models.Index(fields=["client", "status", "due_date"])]


class Playbook(models.Model):
    """The organisation's review guidance for a kind of document: library details (what the Playbooks page shows:
    description, rich content, category, tags, an uploaded document) and structured positions (topic, preferred
    wording, fallbacks, red lines) that AI review checks documents against. Benchmarks it relies on are linked.
    One organisation; see contracts/knowledge.py for access rules and the API."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="playbooks")
    name = models.CharField(max_length=255)
    contract_type = models.CharField(max_length=60, blank=True, default="")
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    # Library details (formerly the Node service's playbooks table)
    description = models.TextField(blank=True, default="")
    content = models.TextField(blank=True, default="")                  # rich text / markdown guidance
    category = models.CharField(max_length=60, blank=True, default="")  # e.g. legal-contracts
    tags = models.JSONField(default=list, blank=True)                   # tag ids
    industry = models.CharField(max_length=100, blank=True, default="")
    document_type = models.CharField(max_length=100, blank=True, default="")
    use_case = models.CharField(max_length=100, blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    source_file = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="playbooks")
    metadata = models.JSONField(default=dict, blank=True)
    legacy_id = models.CharField(max_length=64, blank=True, default="", db_index=True)   # Node playbook id (migration)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    benchmarks = models.ManyToManyField("Benchmark", blank=True, related_name="playbooks")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["client", "legacy_id"], condition=~models.Q(legacy_id=""),
                                               name="uniq_playbook_legacy_per_client")]


class PlaybookPosition(models.Model):
    """One clause topic: the preferred wording, acceptable fallbacks and red lines."""
    SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE, related_name="positions")
    topic = models.CharField(max_length=120)                  # e.g. Limitation of liability
    standard = models.TextField()                             # preferred position / wording
    fallbacks = models.JSONField(default=list, blank=True)    # acceptable alternatives, in order
    red_lines = models.JSONField(default=list, blank=True)    # never acceptable
    guidance = models.TextField(blank=True, default="")
    severity = models.CharField(max_length=6, choices=SEVERITIES, default="medium")
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]


class Benchmark(models.Model):
    """A reference standard the organisation measures documents against: a policy, SLA, master agreement,
    regulation, template or precedent. The reference document itself lives in the document system (file), so it
    keeps its permissions, versions and legal holds; its measurable requirements are BenchmarkTerms."""
    KINDS = [("policy", "Policy"), ("sla", "Service level agreement"), ("msa", "Master agreement"),
             ("standard", "Standard terms"), ("regulation", "Regulation"), ("template", "Template"),
             ("precedent", "Precedent"), ("other", "Other")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="benchmarks")
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=12, choices=KINDS, default="policy")
    description = models.TextField(blank=True, default="")
    file = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="benchmarks")
    category = models.CharField(max_length=60, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    version_label = models.CharField(max_length=40, blank=True, default="")
    effective_date = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["client", "kind"])]

    def __str__(self):
        return self.name


class BenchmarkTerm(models.Model):
    """One measurable requirement of a benchmark, e.g. "uptime >= 99.9 %", "payment within 30 days",
    "liability cap <= 1x annual fees", or a wording requirement ("governed by South African law").
    Numbers are compared in code, never by the model: within target = compliant, within tolerance = deviation,
    beyond tolerance = breach."""
    COMPARATORS = [(">=", "At least"), ("<=", "At most"), ("=", "Exactly"), ("between", "Between"),
                   ("present", "Must be present"), ("absent", "Must not be present")]
    SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    benchmark = models.ForeignKey(Benchmark, on_delete=models.CASCADE, related_name="terms")
    topic = models.CharField(max_length=120)                       # e.g. Service availability
    requirement = models.TextField()                               # the requirement in words
    comparator = models.CharField(max_length=8, choices=COMPARATORS, default="present")
    target_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    target_max = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)   # "between"
    unit = models.CharField(max_length=20, blank=True, default="")  # %, days, hours, ZAR, x fees
    tolerance = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)    # allowed gap
    severity = models.CharField(max_length=6, choices=SEVERITIES, default="medium")
    source_quote = models.TextField(blank=True, default="")         # verbatim from the benchmark document
    order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["order", "id"]


class KnowledgeLink(models.Model):
    """Applies a playbook or a benchmark to a contract, a matter or a document (exactly one of each side).
    A matter's links apply to its documents and contracts; a contract's to its document."""
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="knowledge_links")
    playbook = models.ForeignKey(Playbook, null=True, blank=True, on_delete=models.CASCADE, related_name="links")
    benchmark = models.ForeignKey(Benchmark, null=True, blank=True, on_delete=models.CASCADE, related_name="links")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="knowledge_links")
    matter = models.ForeignKey("core.Matter", null=True, blank=True, on_delete=models.CASCADE, related_name="knowledge_links")
    file = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.CASCADE, related_name="knowledge_links")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(name="knowledge_link_one_source",
                                   condition=(models.Q(playbook__isnull=False, benchmark__isnull=True)
                                              | models.Q(playbook__isnull=True, benchmark__isnull=False))),
            models.CheckConstraint(name="knowledge_link_one_target",
                                   condition=(models.Q(contract__isnull=False, matter__isnull=True, file__isnull=True)
                                              | models.Q(contract__isnull=True, matter__isnull=False, file__isnull=True)
                                              | models.Q(contract__isnull=True, matter__isnull=True, file__isnull=False))),
            models.UniqueConstraint(fields=["playbook", "benchmark", "contract", "matter", "file"],
                                    name="uniq_knowledge_link", nulls_distinct=False),
        ]


class PlaybookReview(models.Model):
    """A deviation / compliance analysis of one document against a playbook and/or benchmarks."""
    STATUSES = [("pending", "Queued"), ("running", "Reviewing"), ("done", "Done"), ("failed", "Failed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="playbook_reviews")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="reviews")
    file = models.ForeignKey("core.File", null=True, on_delete=models.SET_NULL, related_name="playbook_reviews")
    playbook = models.ForeignKey(Playbook, null=True, on_delete=models.SET_NULL, related_name="reviews")
    benchmarks = models.ManyToManyField(Benchmark, blank=True, related_name="reviews")
    matter = models.ForeignKey("core.Matter", null=True, blank=True, on_delete=models.SET_NULL, related_name="reviews")
    status = models.CharField(max_length=8, choices=STATUSES, default="pending")
    error = models.TextField(blank=True, default="")
    summary = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ReviewFinding(models.Model):
    STATUSES = [("compliant", "Matches the playbook"), ("deviation", "Deviates (fallback or negotiable)"),
                ("red_line", "Breaches a red line"), ("missing", "Not in the document"),
                ("unverified", "Could not be verified against the document")]
    review = models.ForeignKey(PlaybookReview, on_delete=models.CASCADE, related_name="findings")
    position = models.ForeignKey(PlaybookPosition, null=True, on_delete=models.SET_NULL, related_name="+")
    benchmark_term = models.ForeignKey(BenchmarkTerm, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    measured_value = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    gap = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)   # measured minus target
    unit = models.CharField(max_length=20, blank=True, default="")
    topic = models.CharField(max_length=120)
    severity = models.CharField(max_length=6, default="medium")
    status = models.CharField(max_length=10, choices=STATUSES)
    quote = models.TextField(blank=True, default="")          # verbatim from the document (verified)
    offset = models.IntegerField(null=True, blank=True)
    explanation = models.TextField(blank=True, default="")
    proposed_text = models.TextField(blank=True, default="")  # suggested redline wording
    confidence = models.FloatField(default=0)

    class Meta:
        ordering = ["id"]


class SignatureRequest(models.Model):
    """A contract sent for e-signature through the sender's connected DocuSign or Adobe Sign account."""
    STATUSES = [("sending", "Sending"), ("sent", "Sent"), ("delivered", "Opened"), ("completed", "Signed"),
                ("declined", "Declined"), ("voided", "Voided"), ("error", "Error")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="signature_requests")
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="signature_requests")
    provider = models.CharField(max_length=20)
    envelope_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="sending")
    signers = models.JSONField(default=list, blank=True)
    source_file = models.ForeignKey("core.File", null=True, on_delete=models.SET_NULL, related_name="+")
    signed_file = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    certificate_file = models.ForeignKey("core.File", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
