"""
Contracts API (/api/v1/contracts/). Every query is scoped by contracts/access.py.

GET  contracts/?stage=&matter=&search=&mine=1&page=     list           POST contracts/      create (managers)
POST intake/                                            request a contract (anyone with contract.request)
GET  contracts/<id>/   PATCH contracts/<id>/            detail / edit (owner or managers)
POST contracts/<id>/advance/ {to, note}                 stage change
POST approvals/<id>/decide/ {decision: approve|reject, note}
GET  approvals/mine/                                    approvals waiting for me
GET  rules/   POST rules/   PATCH rules/<id>/           approval rules (managers)
GET  obligations/?status=&due_before=&mine=1   POST obligations/   PATCH obligations/<id>/ {action|fields}
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_access_file, matters_for_user, tenant_id, user_has_perm
from core.models import File, Matter
from . import access, lifecycle
from .models import STAGES, ApprovalRule, Contract, ContractApproval, Obligation

PAGE = 50
STAGE_KEYS = {k for k, _ in STAGES}


def _err(msg, code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": msg}, status=code)


def _person(u):
    return {"id": u.pk, "email": u.email, "name": (f"{u.first_name} {u.last_name}".strip() or u.email)} if u else None


def contract_out(c, user, full=False):
    data = {"id": str(c.id), "title": c.title, "counterparty": c.counterparty, "contract_type": c.contract_type,
            "stage": c.stage, "stage_label": c.get_stage_display(), "value": str(c.value) if c.value is not None else None,
            "currency": c.currency, "start_date": c.start_date, "end_date": c.end_date, "notice_days": c.notice_days,
            "auto_renew": c.auto_renew, "renewal_term_months": c.renewal_term_months, "needed_by": c.needed_by,
            "matter": {"id": str(c.matter_id), "name": c.matter.name} if c.matter_id else None,
            "document": {"id": c.document_id, "filename": c.document.filename} if c.document_id else None,
            "owner": _person(c.owner), "requested_by": _person(c.requested_by),
            "created_at": c.created_at, "updated_at": c.updated_at, "can_edit": access.can_edit(user, c)}
    if full:
        data["description"] = c.description
        data["next_stages"] = sorted(lifecycle.TRANSITIONS.get(c.stage, set())) if data["can_edit"] else []
        data["approvals"] = [approval_out(a, user) for a in c.approvals.exclude(status="void").select_related("approver", "decided_by")]
        data["events"] = [{"kind": e.kind, "from": e.from_stage, "to": e.to_stage, "note": e.note, "details": e.details,
                           "actor": e.actor.email if e.actor else "system", "at": e.at}
                          for e in c.events.select_related("actor")]
        data["obligations"] = [obligation_out(o) for o in c.obligations.select_related("owner")]
    return data


def approval_out(a, user):
    return {"id": a.pk, "order": a.order, "status": a.status, "approver": _person(a.approver), "approver_role": a.approver_role,
            "decided_by": a.decided_by.email if a.decided_by else None, "decided_at": a.decided_at, "note": a.note,
            "can_decide": lifecycle.can_decide(user, a)}


def obligation_out(o):
    today = date.today()
    return {"id": str(o.id), "title": o.title, "description": o.description, "kind": o.kind, "due_date": o.due_date,
            "recurrence": o.recurrence, "reminder_days": o.reminder_days, "status": o.status, "automatic": o.automatic,
            "overdue": o.status == "open" and o.due_date < today, "owner": _person(o.owner),
            "contract": {"id": str(o.contract_id), "title": o.contract.title} if o.contract_id else None,
            "matter": {"id": str(o.matter_id), "name": o.matter.name} if o.matter_id else None,
            "completed_at": o.completed_at}


# ── field validation shared by create, intake and edit ───────────────────────────────────────
def clean_fields(data, user, partial=False):
    tid = tenant_id(user)
    out = {}
    for f, size in (("title", 255), ("counterparty", 255), ("contract_type", 60), ("description", 20000)):
        if f in data or (f == "title" and not partial):
            out[f] = str(data.get(f) or "").strip()[:size]
    if "title" in out and not out["title"]:
        raise ValueError("Give the contract a title.")
    if "value" in data:
        if data["value"] in (None, ""):
            out["value"] = None
        else:
            try:
                v = Decimal(str(data["value"]))
            except InvalidOperation:
                raise ValueError("Value must be a number.")
            if not v.is_finite() or v < 0 or v > Decimal("1e13"):
                raise ValueError("Value must be a positive amount.")
            out["value"] = v.quantize(Decimal("0.01"))
    if "currency" in data:
        cur = str(data["currency"] or "").strip().upper()
        if cur and len(cur) != 3:
            raise ValueError("Currency must be a three-letter code such as ZAR or USD.")
        out["currency"] = cur
    for f in ("start_date", "end_date", "needed_by"):
        if f in data:
            try:
                out[f] = date.fromisoformat(data[f]) if data[f] else None
            except (TypeError, ValueError):
                raise ValueError(f"{f.replace('_', ' ').capitalize()} must be a date (YYYY-MM-DD).")
    if out.get("start_date") and out.get("end_date") and out["end_date"] < out["start_date"]:
        raise ValueError("The end date is before the start date.")
    for f in ("notice_days", "renewal_term_months"):
        if f in data:
            try:
                out[f] = int(data[f]) if data[f] not in (None, "") else None
            except (TypeError, ValueError):
                raise ValueError(f"{f.replace('_', ' ')} must be a whole number.")
            if out[f] is not None and not 0 <= out[f] <= 3650:
                raise ValueError(f"{f.replace('_', ' ')} is out of range.")
    if "auto_renew" in data:
        v = data["auto_renew"]
        out["auto_renew"] = v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)
    if "matter_id" in data:
        if data["matter_id"]:
            matter = matters_for_user(user).filter(pk=data["matter_id"], client_id=tid).first()
            if matter is None:
                raise LookupError("Matter not found.")
            out["matter"] = matter
        else:
            out["matter"] = None
    if "document_id" in data:
        if data["document_id"]:
            doc = File.objects.select_related("user").filter(pk=data["document_id"], user__client_id=tid).first()
            if doc is None or not can_access_file(user, doc, "view"):
                raise LookupError("Document not found.")
            out["document"] = doc
        else:
            out["document"] = None
    if "owner_id" in data:
        if data["owner_id"]:
            owner = get_user_model().objects.filter(pk=data["owner_id"], client_id=tid, is_active=True).first()
            if owner is None:
                raise LookupError("Owner not found in your organisation.")
            out["owner"] = owner
        else:
            out["owner"] = None
    return out


def _fields_or_error(data, user, partial=False):
    try:
        return clean_fields(data, user, partial), None
    except ValueError as exc:
        return None, _err(str(exc))
    except LookupError as exc:
        return None, _err(str(exc), status.HTTP_404_NOT_FOUND)


class ContractListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = access.contracts_for(request.user).select_related("matter", "document", "owner", "requested_by")
        qp = request.query_params
        if qp.get("stage") in STAGE_KEYS:
            qs = qs.filter(stage=qp["stage"])
        elif qp.get("stage") == "open":
            qs = qs.exclude(stage__in=["expired", "terminated"])
        if qp.get("matter"):
            qs = qs.filter(matter_id=qp["matter"])
        if qp.get("mine") == "1":
            qs = qs.filter(Q(owner=request.user) | Q(requested_by=request.user))
        if qp.get("search"):
            term = qp["search"][:100]
            qs = qs.filter(Q(title__icontains=term) | Q(counterparty__icontains=term) | Q(contract_type__icontains=term))
        try:
            page = max(int(qp.get("page", 1)), 1)
        except ValueError:
            page = 1
        total = qs.count()
        rows = qs[(page - 1) * PAGE: page * PAGE]
        counts = dict(access.contracts_for(request.user).order_by().values_list("stage").annotate(n=Count("id")))
        return Response({"results": [contract_out(c, request.user) for c in rows], "count": total, "page": page,
                         "total_pages": max(1, -(-total // PAGE)), "stage_counts": counts,
                         "can_manage": access.is_manager(request.user),
                         "can_request": user_has_perm(request.user, "contract.request") or access.is_manager(request.user)})

    def post(self, request):
        if not access.is_manager(request.user):
            return _err("Only contract managers can create contracts directly; use a contract request instead.", 403)
        fields, error = _fields_or_error(request.data, request.user)
        if error:
            return error
        stage = request.data.get("stage") if request.data.get("stage") in ("request", "draft") else "draft"
        c = Contract.objects.create(client_id=tenant_id(request.user), requested_by=request.user, stage=stage,
                                    **{"owner": request.user, **fields})
        lifecycle.record(c, "created", request.user, to_stage=stage)
        return Response(contract_out(c, request.user, full=True), status=201)


class IntakeView(APIView):
    """A contract request from anyone allowed to ask; contract managers pick it up."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not (user_has_perm(request.user, "contract.request") or access.is_manager(request.user)):
            return _err("You cannot request contracts.", 403)
        if tenant_id(request.user) is None:
            return _err("Your account is not part of an organisation.")
        allowed = {k: v for k, v in request.data.items() if k in ("title", "counterparty", "contract_type", "description",
                                                                 "value", "currency", "needed_by", "matter_id",
                                                                 "document_id", "start_date", "end_date")}
        fields, error = _fields_or_error(allowed, request.user)
        if error:
            return error
        c = Contract.objects.create(client_id=tenant_id(request.user), requested_by=request.user, stage="request", **fields)
        lifecycle.record(c, "created", request.user, to_stage="request", note="Contract requested.")
        return Response(contract_out(c, request.user, full=True), status=201)


def _contract(user, pk):
    try:
        return access.contracts_for(user).select_related("matter", "document", "owner", "requested_by").get(pk=pk)
    except Exception:
        return None


class ContractDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        c = _contract(request.user, pk)
        return Response(contract_out(c, request.user, full=True)) if c else _err("Contract not found.", 404)

    def patch(self, request, pk):
        c = _contract(request.user, pk)
        if c is None:
            return _err("Contract not found.", 404)
        if not access.can_edit(request.user, c):
            return _err("Only the owner or a contract manager can edit this contract.", 403)
        if c.stage in ("expired", "terminated"):
            return _err("A closed contract cannot be edited.")
        data = dict(request.data)
        if "owner_id" in data and not access.is_manager(request.user):
            return _err("Only contract managers can reassign a contract.", 403)
        fields, error = _fields_or_error(data, request.user, partial=True)
        if error:
            return error
        changed = {}
        for k, v in fields.items():
            if getattr(c, k) != v:
                changed[k] = str(v.pk if hasattr(v, "pk") else v)
                setattr(c, k, v)
        if changed:
            c.save()
            lifecycle.record(c, "edit", request.user, changes=changed)
            if c.stage in ("active", "renewal") and changed.keys() & {"end_date", "notice_days"}:
                lifecycle.sync_obligations(c, request.user)
        return Response(contract_out(c, request.user, full=True))


class AdvanceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        c = _contract(request.user, pk)
        if c is None:
            return _err("Contract not found.", 404)
        if not access.can_edit(request.user, c):
            return _err("Only the owner or a contract manager can move this contract.", 403)
        try:
            c = lifecycle.advance(c, str(request.data.get("to") or ""), request.user, str(request.data.get("note") or ""))
        except lifecycle.LifecycleError as exc:
            return _err(str(exc))
        return Response(contract_out(_contract(request.user, c.pk) or c, request.user, full=True))


class DecideView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        a = ContractApproval.objects.select_related("contract").filter(pk=pk).first()
        if a is None or not access.can_view(request.user, a.contract):
            return _err("Approval not found.", 404)
        decision = request.data.get("decision")
        if decision not in ("approve", "reject"):
            return _err("decision must be approve or reject.")
        try:
            lifecycle.decide(a, request.user, decision == "approve", str(request.data.get("note") or ""))
        except lifecycle.LifecycleError as exc:
            return _err(str(exc), 403)
        return Response(contract_out(_contract(request.user, a.contract_id), request.user, full=True))


class MyApprovalsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pending = ContractApproval.objects.filter(status="pending", contract__stage="approval",
                                                  contract__in=access.contracts_for(request.user).values("pk")) \
            .select_related("contract", "approver")
        mine = [a for a in pending if lifecycle.can_decide(request.user, a)]
        return Response({"results": [{**approval_out(a, request.user), "contract": {"id": str(a.contract_id),
                                      "title": a.contract.title, "value": str(a.contract.value) if a.contract.value is not None else None,
                                      "currency": a.contract.currency}} for a in mine]})


def rule_out(r):
    return {"id": r.pk, "name": r.name, "contract_type": r.contract_type, "order": r.order, "active": r.active,
            "min_value": str(r.min_value) if r.min_value is not None else None,
            "approver": _person(r.approver), "approver_role": r.approver_role}


class RulesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not access.is_manager(request.user):
            return _err("Only contract managers can see approval rules.", 403)
        return Response({"results": [rule_out(r) for r in ApprovalRule.objects.filter(client_id=tenant_id(request.user))
                                     .select_related("approver")]})

    def post(self, request, pk=None):
        if not access.is_manager(request.user):
            return _err("Only contract managers can change approval rules.", 403)
        tid = tenant_id(request.user)
        name = str(request.data.get("name") or "").strip()[:255]
        approver = None
        if request.data.get("approver_id"):
            approver = get_user_model().objects.filter(pk=request.data["approver_id"], client_id=tid, is_active=True).first()
            if approver is None:
                return _err("Approver not found in your organisation.", 404)
        role = str(request.data.get("approver_role") or "")
        from custom_authentication.rbac import ROLE_DEFINITIONS
        if role and role not in ROLE_DEFINITIONS:
            return _err("Unknown role.")
        if not name or not (approver or role):
            return _err("Give the rule a name and an approver (a person or a role).")
        try:
            min_value = Decimal(str(request.data["min_value"])) if request.data.get("min_value") not in (None, "") else None
            order = int(request.data.get("order") or 1)
        except (InvalidOperation, ValueError):
            return _err("Minimum value and order must be numbers.")
        r = ApprovalRule.objects.create(client_id=tid, name=name, contract_type=str(request.data.get("contract_type") or "")[:60],
                                        min_value=min_value, approver=approver, approver_role=role, order=order)
        return Response(rule_out(r), status=201)

    def patch(self, request, pk):
        if not access.is_manager(request.user):
            return _err("Only contract managers can change approval rules.", 403)
        r = ApprovalRule.objects.filter(pk=pk, client_id=tenant_id(request.user)).first()
        if r is None:
            return _err("Rule not found.", 404)
        if "active" in request.data:
            r.active = bool(request.data["active"]) if not isinstance(request.data["active"], str) \
                else request.data["active"].lower() in ("1", "true", "yes", "on")
            r.save(update_fields=["active"])
        return Response(rule_out(r))


class ObligationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = access.obligations_for(request.user).select_related("owner", "contract", "matter")
        qp = request.query_params
        if qp.get("status") in ("open", "done", "waived"):
            qs = qs.filter(status=qp["status"])
        if qp.get("due_before"):
            try:
                qs = qs.filter(due_date__lte=date.fromisoformat(qp["due_before"]))
            except ValueError:
                return _err("due_before must be a date (YYYY-MM-DD).")
        if qp.get("mine") == "1":
            qs = qs.filter(owner=request.user)
        if qp.get("contract"):
            qs = qs.filter(contract_id=qp["contract"])
        return Response({"results": [obligation_out(o) for o in qs[:500]]})

    def post(self, request):
        user, tid = request.user, tenant_id(request.user)
        if tid is None:
            return _err("Your account is not part of an organisation.")
        title = str(request.data.get("title") or "").strip()[:255]
        try:
            due = date.fromisoformat(request.data.get("due_date"))
        except (TypeError, ValueError):
            return _err("Give a due date (YYYY-MM-DD).")
        if not title:
            return _err("Give the obligation a title.")
        contract = None
        if request.data.get("contract_id"):
            contract = _contract(user, request.data["contract_id"])
            if contract is None:
                return _err("Contract not found.", 404)
            if not access.can_edit(user, contract):
                return _err("Only the owner or a contract manager can add obligations to this contract.", 403)
        elif not (user_has_perm(user, "contract.request") or access.is_manager(user)):
            return _err("You cannot add obligations.", 403)
        fields, error = _fields_or_error({k: request.data[k] for k in ("matter_id", "owner_id") if k in request.data}, user, partial=True)
        if error:
            return error
        kind = request.data.get("kind") if request.data.get("kind") in dict(Obligation.KINDS) else "obligation"
        recurrence = request.data.get("recurrence") if request.data.get("recurrence") in dict(Obligation.RECURRENCES) else "none"
        try:
            reminder = min(max(int(request.data.get("reminder_days", 14)), 0), 365)
        except (TypeError, ValueError):
            return _err("reminder_days must be a whole number.")
        o = Obligation.objects.create(client_id=tid, contract=contract, matter=fields.get("matter") or (contract.matter if contract else None),
                                      title=title, description=str(request.data.get("description") or "")[:5000], kind=kind,
                                      due_date=due, recurrence=recurrence, reminder_days=reminder,
                                      owner=fields.get("owner") or user, created_by=user)
        if contract:
            lifecycle.record(contract, "obligation", user, note=f"Obligation added: {title}")
        return Response(obligation_out(o), status=201)


class ObligationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        o = access.obligations_for(request.user).select_related("contract", "owner").filter(pk=pk).first()
        if o is None:
            return _err("Obligation not found.", 404)
        user = request.user
        editor = access.is_manager(user) or o.owner_id == user.pk or o.created_by_id == user.pk or \
            (o.contract_id and access.can_edit(user, o.contract))
        if not editor:
            return _err("Only the obligation's owner or a contract manager can change it.", 403)
        action = request.data.get("action")
        if action in ("done", "waive"):
            if o.status != "open":
                return _err("This obligation is already closed.")
            nxt = lifecycle.complete(o, user, waive=action == "waive")
            if o.contract_id:
                lifecycle.record(o.contract, "obligation", user, note=f"Obligation {'waived' if action == 'waive' else 'done'}: {o.title}")
            return Response({**obligation_out(o), "next": obligation_out(nxt) if nxt else None})
        if "due_date" in request.data:
            try:
                o.due_date = date.fromisoformat(request.data["due_date"])
            except (TypeError, ValueError):
                return _err("due_date must be a date (YYYY-MM-DD).")
        if "owner_id" in request.data:
            fields, error = _fields_or_error({"owner_id": request.data["owner_id"]}, user, partial=True)
            if error:
                return error
            o.owner = fields.get("owner")
        if "reminder_days" in request.data:
            try:
                o.reminder_days = min(max(int(request.data["reminder_days"]), 0), 365)
            except (TypeError, ValueError):
                return _err("reminder_days must be a whole number.")
        o.save()
        return Response(obligation_out(o))
