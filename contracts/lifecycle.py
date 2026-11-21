"""
Contract lifecycle rules: allowed stage changes, approval routing, automatic obligations, the daily run.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import ApprovalRule, Contract, ContractApproval, ContractEvent, Obligation

logger = logging.getLogger(__name__)

# Manual moves (the system also moves: approval -> signature / review, active <-> renewal, -> expired).
TRANSITIONS = {
    "request": {"draft", "terminated"},
    "draft": {"review", "terminated"},
    "review": {"draft", "approval", "terminated"},
    "approval": {"review", "terminated"},
    "signature": {"active", "review", "terminated"},
    "active": {"renewal", "terminated"},
    "renewal": {"active", "expired", "terminated"},
    "expired": set(),
    "terminated": set(),
}


class LifecycleError(ValueError):
    """A refused change, with a message safe to show."""


def record(contract, kind, actor=None, from_stage="", to_stage="", note="", **details):
    ContractEvent.objects.create(contract=contract, kind=kind, actor=actor, from_stage=from_stage, to_stage=to_stage,
                                 note=note[:5000], details=details)


def add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


# ── approvals ─────────────────────────────────────────────────────────────────────────────────
def route_approvals(contract, actor):
    """Create approval steps from the organisation's rules. Returns how many are needed."""
    contract.approvals.filter(status="pending").update(status="void")
    steps = [r for r in ApprovalRule.objects.filter(client_id=contract.client_id, active=True) if r.applies_to(contract)]
    for rule in steps:
        ContractApproval.objects.create(contract=contract, rule=rule, approver=rule.approver,
                                        approver_role=rule.approver_role, order=rule.order)
    record(contract, "approval", actor, note=f"{len(steps)} approval(s) requested", steps=[r.name for r in steps])
    return len(steps)


def can_decide(user, approval) -> bool:
    from core.access import tenant_id, user_role_names
    contract = approval.contract
    if approval.status != "pending" or contract.stage != "approval" or tenant_id(user) != contract.client_id:
        return False
    if contract.requested_by_id == user.pk or contract.owner_id == user.pk:
        return False   # separation of duties: nobody approves their own contract
    if approval.approver_id:
        return approval.approver_id == user.pk
    return bool(approval.approver_role) and approval.approver_role in user_role_names(user)


@transaction.atomic
def decide(approval, user, approve: bool, note=""):
    approval = ContractApproval.objects.select_for_update().select_related("contract").get(pk=approval.pk)
    if not can_decide(user, approval):
        raise LifecycleError("You cannot decide this approval.")
    approval.status = "approved" if approve else "rejected"
    approval.decided_by, approval.decided_at, approval.note = user, timezone.now(), note[:5000]
    approval.save(update_fields=["status", "decided_by", "decided_at", "note"])
    contract = Contract.objects.select_for_update().get(pk=approval.contract_id)
    record(contract, "approval", user, note=note, decision=approval.status, step=approval.order)
    if not approve:
        contract.approvals.filter(status="pending").update(status="void")
        _move(contract, "review", user, note="Returned to review after a rejected approval.")
    elif not contract.approvals.filter(status="pending").exists():
        _move(contract, "signature", user, note="All approvals given.")
    return approval


# ── stage changes ─────────────────────────────────────────────────────────────────────────────
def _move(contract, to_stage, actor, note=""):
    from_stage = contract.stage
    contract.stage = to_stage
    contract.save(update_fields=["stage", "updated_at"])
    record(contract, "stage", actor, from_stage, to_stage, note)
    if to_stage == "active":
        sync_obligations(contract, actor)
    if to_stage in ("expired", "terminated"):
        contract.obligations.filter(status="open", automatic=True).update(status="waived")


@transaction.atomic
def advance(contract, to_stage, actor, note=""):
    contract = Contract.objects.select_for_update().get(pk=contract.pk)
    if to_stage not in TRANSITIONS.get(contract.stage, set()):
        raise LifecycleError(f"A contract cannot move from {contract.get_stage_display()} to {to_stage}.")
    if to_stage == "draft" and contract.owner_id is None:
        contract.owner = actor
        contract.save(update_fields=["owner"])
    if to_stage == "approval":
        if route_approvals(contract, actor) == 0:
            # No rule applies: approval is not needed, go straight to signature.
            _move(contract, "approval", actor, note)
            _move(contract, "signature", actor, note="No approval rule applies.")
            return contract
    if to_stage == "active":
        if not contract.document_id:
            raise LifecycleError("Attach the signed document before making the contract active.")
        if not contract.start_date:
            raise LifecycleError("Set the start date before making the contract active.")
    if to_stage == "active" and contract.stage == "renewal":
        if not contract.end_date or contract.end_date <= timezone.localdate():
            raise LifecycleError("Set the new end date before renewing.")
    if contract.stage == "approval" and to_stage == "review":
        contract.approvals.filter(status="pending").update(status="void")
    _move(contract, to_stage, actor, note)
    return contract


# ── obligations from the contract's dates ────────────────────────────────────────────────────
def sync_obligations(contract, actor=None):
    """Keep the automatic renewal-notice and expiry obligations in step with the contract's dates."""
    if contract.stage in ("expired", "terminated"):
        return
    wanted = {}
    if contract.end_date:
        wanted["expiry"] = (contract.end_date, f"{contract.title}: contract ends")
        if contract.notice_days:
            wanted["renewal"] = (contract.end_date - timedelta(days=contract.notice_days),
                                 f"{contract.title}: decide on renewal (notice deadline)")
    for kind, (due, title) in wanted.items():
        existing = contract.obligations.filter(kind=kind, automatic=True, status="open").first()
        if existing:
            if existing.due_date != due:
                existing.due_date, existing.title = due, title
                existing.save(update_fields=["due_date", "title"])
        else:
            Obligation.objects.create(client_id=contract.client_id, contract=contract, matter=contract.matter,
                                      title=title, kind=kind, due_date=due, owner=contract.owner, automatic=True,
                                      created_by=actor)
    contract.obligations.filter(automatic=True, status="open").exclude(kind__in=list(wanted)).update(status="waived")


def complete(obligation, user, waive=False):
    """Mark done (or waived); a recurring obligation gets its next occurrence."""
    obligation.status = "waived" if waive else "done"
    obligation.completed_by, obligation.completed_at = user, timezone.now()
    obligation.save(update_fields=["status", "completed_by", "completed_at"])
    step = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(obligation.recurrence)
    if step and not waive:
        return Obligation.objects.create(
            client_id=obligation.client_id, contract=obligation.contract, matter=obligation.matter,
            title=obligation.title, description=obligation.description, kind=obligation.kind,
            due_date=add_months(obligation.due_date, step), recurrence=obligation.recurrence,
            reminder_days=obligation.reminder_days, owner=obligation.owner, created_by=user)
    return None


# ── daily run ────────────────────────────────────────────────────────────────────────────────
def run_daily(today=None):
    today = today or timezone.localdate()
    moved = {"renewal": 0, "expired": 0, "renewed": 0, "reminders": 0}
    for c in Contract.objects.filter(stage="active", end_date__isnull=False, notice_days__isnull=False):
        if today >= c.end_date - timedelta(days=c.notice_days):
            with transaction.atomic():
                _move(c, "renewal", None, note="Notice window reached.")
            moved["renewal"] += 1
    for c in Contract.objects.filter(stage__in=["active", "renewal"], end_date__lt=today):
        with transaction.atomic():
            if c.auto_renew and c.renewal_term_months:
                old = c.end_date
                while c.end_date < today:
                    c.end_date = add_months(c.end_date, c.renewal_term_months)
                c.stage = "active"
                c.save(update_fields=["end_date", "stage", "updated_at"])
                record(c, "renewal", None, note="Renewed automatically.", previous_end=str(old), new_end=str(c.end_date))
                sync_obligations(c)
                moved["renewed"] += 1
            else:
                _move(c, "expired", None, note="End date passed.")
                moved["expired"] += 1
    moved["reminders"] = send_reminders(today)
    return moved


def send_reminders(today):
    """Email the owner of each open obligation entering its reminder window (once a day)."""
    from email_management.models import OutboxEmail
    count = 0
    for o in Obligation.objects.filter(status="open", owner__isnull=False, owner__is_active=True).select_related("owner", "contract"):
        if o.due_date - timedelta(days=o.reminder_days) > today or o.last_reminded_on == today:
            continue
        overdue = o.due_date < today
        subject = f"{'Overdue' if overdue else 'Due ' + o.due_date.isoformat()}: {o.title}"[:255]
        body = (f"{o.title}\n\nDue: {o.due_date.isoformat()}{' (overdue)' if overdue else ''}\n"
                f"{('Contract: ' + o.contract.title) if o.contract_id else ''}\n\n"
                "Open aiDocuMines, Contracts, Obligations to mark it done.")
        try:
            out = OutboxEmail.objects.create(user=o.owner, client_id=str(o.client_id), to=[o.owner.email],
                                             subject=subject, body_text=body)
            from email_management.tasks import send_outbox_email
            send_outbox_email.delay(out.pk)
        except Exception:
            logger.warning("Obligation reminder could not be queued for %s", o.pk, exc_info=True)
            continue
        Obligation.objects.filter(pk=o.pk).update(last_reminded_on=today)
        count += 1
    return count


@shared_task(name="contracts.daily", ignore_result=True, soft_time_limit=600, time_limit=660)
def contracts_daily():
    logger.info("Contracts daily run: %s", run_daily())
