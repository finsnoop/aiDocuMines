"""
Playbooks and AI playbook review (/api/v1/contracts/).

GET  playbooks/?active=1        POST playbooks/ {name, contract_type}            (create: contract managers)
GET  playbooks/<id>/            PATCH playbooks/<id>/ {name, contract_type, active}
POST playbooks/<id>/positions/  PATCH/DELETE playbooks/<id>/positions/<pid>/    (contract managers)
POST reviews/ {playbook_id, contract_id | file_id}   queue an AI review (contract owner/manager, or anyone who
                                                     may process the document)
GET  reviews/<id>/              GET contracts/<id>/reviews/
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_access_file, tenant_id
from core.models import File
from . import access
from .models import Playbook, PlaybookPosition, PlaybookReview
from .views import _contract, _err

MAX_POSITIONS = 60


def _str_list(value, limit=10):
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:2000] for v in value if str(v).strip()][:limit]


def position_out(p):
    return {"id": p.pk, "topic": p.topic, "standard": p.standard, "fallbacks": p.fallbacks, "red_lines": p.red_lines,
            "guidance": p.guidance, "severity": p.severity, "order": p.order}


def playbook_out(pb, positions=True):
    data = {"id": str(pb.id), "name": pb.name, "contract_type": pb.contract_type, "active": pb.active,
            "position_count": pb.positions.count()}
    if positions:
        data["positions"] = [position_out(p) for p in pb.positions.all()]
    return data


def review_out(r):
    return {"id": str(r.id), "status": r.status, "error": r.error, "summary": r.summary, "created_at": r.created_at,
            "finished_at": r.finished_at,
            "playbook": {"id": str(r.playbook_id), "name": r.playbook.name} if r.playbook_id else None,
            "file": {"id": r.file_id, "filename": r.file.filename} if r.file_id else None,
            "contract": {"id": str(r.contract_id), "title": r.contract.title} if r.contract_id else None,
            "findings": [{"topic": f.topic, "severity": f.severity, "status": f.status, "status_label": f.get_status_display(),
                          "quote": f.quote, "explanation": f.explanation, "proposed_text": f.proposed_text,
                          "confidence": round(f.confidence, 2)} for f in r.findings.all()]}


def _playbook(user, pk):
    tid = tenant_id(user)
    if not tid or not pk:
        return None
    try:
        return Playbook.objects.filter(pk=pk, client_id=tid).first()
    except Exception:   # malformed id
        return None


def _bool(v):
    return v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)


class PlaybookListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = Playbook.objects.filter(client_id=tenant_id(request.user))
        if request.query_params.get("active") == "1":
            rows = rows.filter(active=True)
        return Response({"results": [playbook_out(p, positions=False) for p in rows],
                         "can_manage": access.is_manager(request.user)})

    def post(self, request):
        if not access.is_manager(request.user):
            return _err("Only contract managers can create playbooks.", 403)
        name = str(request.data.get("name") or "").strip()[:255]
        if not name:
            return _err("Give the playbook a name.")
        pb = Playbook.objects.create(client_id=tenant_id(request.user), name=name, created_by=request.user,
                                     contract_type=str(request.data.get("contract_type") or "")[:60])
        return Response(playbook_out(pb), status=201)


class PlaybookDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        pb = _playbook(request.user, pk)
        return Response(playbook_out(pb)) if pb else _err("Playbook not found.", 404)

    def patch(self, request, pk):
        pb = _playbook(request.user, pk)
        if pb is None:
            return _err("Playbook not found.", 404)
        if not access.is_manager(request.user):
            return _err("Only contract managers can change playbooks.", 403)
        if str(request.data.get("name") or "").strip():
            pb.name = str(request.data["name"]).strip()[:255]
        if "contract_type" in request.data:
            pb.contract_type = str(request.data["contract_type"] or "")[:60]
        if "active" in request.data:
            pb.active = _bool(request.data["active"])
        pb.save()
        return Response(playbook_out(pb))


class PositionView(APIView):
    permission_classes = [IsAuthenticated]

    def _guard(self, request, pk):
        pb = _playbook(request.user, pk)
        if pb is None:
            return None, _err("Playbook not found.", 404)
        if not access.is_manager(request.user):
            return None, _err("Only contract managers can change playbooks.", 403)
        return pb, None

    @staticmethod
    def _fields(data, partial=False):
        out = {}
        if "topic" in data or not partial:
            out["topic"] = str(data.get("topic") or "").strip()[:120]
            if not out["topic"]:
                raise ValueError("Give the position a topic, such as Limitation of liability.")
        if "standard" in data or not partial:
            out["standard"] = str(data.get("standard") or "").strip()[:10000]
            if not out["standard"]:
                raise ValueError("Describe the preferred position.")
        for f in ("fallbacks", "red_lines"):
            if f in data:
                out[f] = _str_list(data[f])
        if "guidance" in data:
            out["guidance"] = str(data["guidance"] or "")[:5000]
        if "severity" in data:
            if data["severity"] not in ("low", "medium", "high"):
                raise ValueError("severity must be low, medium or high.")
            out["severity"] = data["severity"]
        if "order" in data:
            out["order"] = max(int(data["order"]), 1)
        return out

    def post(self, request, pk):
        pb, error = self._guard(request, pk)
        if error:
            return error
        try:
            fields = self._fields(request.data)
        except (ValueError, TypeError) as exc:
            return _err(str(exc))
        if pb.positions.count() >= MAX_POSITIONS:
            return _err(f"A playbook can have at most {MAX_POSITIONS} positions.")
        return Response(position_out(PlaybookPosition.objects.create(playbook=pb, **fields)), status=201)

    def patch(self, request, pk, position_id):
        pb, error = self._guard(request, pk)
        if error:
            return error
        p = pb.positions.filter(pk=position_id).first()
        if p is None:
            return _err("Position not found.", 404)
        try:
            for k, v in self._fields(request.data, partial=True).items():
                setattr(p, k, v)
        except (ValueError, TypeError) as exc:
            return _err(str(exc))
        p.save()
        return Response(position_out(p))

    def delete(self, request, pk, position_id):
        pb, error = self._guard(request, pk)
        if error:
            return error
        pb.positions.filter(pk=position_id).delete()
        return Response(status=204)


class ReviewStartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user, tid = request.user, tenant_id(request.user)
        pb = _playbook(user, request.data.get("playbook_id"))
        if pb is None or not pb.active:
            return _err("Playbook not found.", 404)
        if not pb.positions.exists():
            return _err("Add positions to the playbook first.")
        contract, doc = None, None
        if request.data.get("contract_id"):
            contract = _contract(user, request.data["contract_id"])
            if contract is None:
                return _err("Contract not found.", 404)
            if not access.can_edit(user, contract):
                return _err("Only the owner or a contract manager can review this contract.", 403)
            doc = contract.document
            if doc is None:
                return _err("Attach the contract document first.")
        elif request.data.get("file_id"):
            doc = File.objects.select_related("user").filter(pk=request.data["file_id"], user__client_id=tid).first()
        if doc is None or not can_access_file(user, doc, "process"):
            return _err("Document not found.", 404)
        from .playbook import playbook_review_task
        review = PlaybookReview.objects.create(client_id=tid, contract=contract, file=doc, playbook=pb, requested_by=user)
        playbook_review_task.delay(str(review.pk), user_id=user.pk)
        return Response(review_out(review), status=202)


class ReviewDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        r = PlaybookReview.objects.select_related("playbook", "file", "contract") \
            .filter(pk=pk, client_id=tenant_id(request.user)).first()
        if r is None:
            return _err("Review not found.", 404)
        allowed = (r.requested_by_id == request.user.pk or access.is_manager(request.user)
                   or (r.contract_id and access.can_view(request.user, r.contract))
                   or (not r.contract_id and r.file_id and can_access_file(request.user, r.file, "view")))
        return Response(review_out(r)) if allowed else _err("Review not found.", 404)


class ContractReviewsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        c = _contract(request.user, pk)
        if c is None:
            return _err("Contract not found.", 404)
        return Response({"results": [review_out(r) for r in c.reviews.select_related("playbook", "file", "contract")[:20]]})
