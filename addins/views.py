"""
Office add-ins API (/api/v1/addins/). The add-ins sign in as the person (same OAuth flow as the web app),
so every call carries that person's organisation, role, matter access, legal holds and ethical walls.

Outlook
  GET  outlook/suggest/?subject=&sender=&recipients=      matters ranked for this email (name or reference in the
                                                            subject, where the sender's domain was filed before, recent use)
  POST outlook/file/  multipart: matter_id, email (.eml), attachments[], subject, sender, sent_at
                                                            files the email and its attachments into the matter through
                                                            the shared ingestion pipeline (core/ingest.py)
Word
  POST word/save/     multipart: document (.docx) and file_id (new version of a stored document)
                                 or matter_id + filename (new document in a matter)
  GET  word/clauses/?search=                                the organisation's clause library
"""
import re
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db.models import F, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import get_file_for_user, matters_for_user, safe_filename, tenant_id, user_has_perm
from .models import FilingHint

MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 150 * 1024 * 1024
MAX_ATTACHMENTS = 50
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.\-]{1,}")


def _err(msg, code=status.HTTP_400_BAD_REQUEST):
    return Response({"error": msg}, status=code)


def _domain(addr):
    m = re.search(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", str(addr or ""))
    return m.group(1).lower() if m else ""


class OutlookSuggestView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user, tid = request.user, tenant_id(request.user)
        if tid is None:
            return Response({"results": []})
        subject = str(request.query_params.get("subject") or "")[:500]
        sender = _domain(request.query_params.get("sender"))
        text = subject.lower()
        words = {w.lower() for w in WORD.findall(subject) if len(w) > 2}
        hints = dict(FilingHint.objects.filter(client_id=tid, sender_domain=sender).values_list("matter_id", "count")) if sender else {}
        scored = []
        for m in matters_for_user(user).filter(client_id=tid).order_by("-updated_at")[:300]:
            score, reasons = 0.0, []
            name = (m.name or "").lower()
            ref = (m.reference or "").lower()
            if name and name in text:
                score += 10; reasons.append("matter name in the subject")
            elif name and words & {w for w in WORD.findall(name) if len(w) > 2}:
                score += 3; reasons.append("matter words in the subject")
            if ref and ref in text:
                score += 12; reasons.append("matter reference in the subject")
            if hints.get(m.pk):
                score += min(hints[m.pk], 10) * 1.5; reasons.append(f"emails from {sender} filed here before")
            scored.append((score, m, reasons))
        scored.sort(key=lambda s: (-s[0], -s[1].updated_at.timestamp()))
        return Response({"results": [{"id": str(m.pk), "name": m.name, "reference": m.reference, "score": round(s, 1),
                                      "reasons": r} for s, m, r in scored[:8]]})


class OutlookFileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from core.ingest import MatterBatch
        user, tid = request.user, tenant_id(request.user)
        if not (user_has_perm(user, "document.upload") or user_has_perm(user, "document.create")):
            return _err("You cannot add documents.", 403)
        matter = matters_for_user(user).filter(pk=request.data.get("matter_id"), client_id=tid).first() \
            if request.data.get("matter_id") else None
        if matter is None:
            return _err("Matter not found.", 404)
        email = request.FILES.get("email")
        attachments = request.FILES.getlist("attachments")
        if email is None:
            return _err("The email is missing.")
        if len(attachments) > MAX_ATTACHMENTS:
            return _err(f"At most {MAX_ATTACHMENTS} attachments can be filed at once.")
        files = [email, *attachments]
        if any(f.size > MAX_FILE for f in files) or sum(f.size for f in files) > MAX_TOTAL:
            return _err("The email or an attachment is too large (50 MB each, 150 MB in total).")
        subject = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", str(request.data.get("subject") or "Email"))[:120].strip() or "Email"
        try:
            sent = datetime.fromisoformat(str(request.data.get("sent_at") or "").replace("Z", "+00:00"))
        except ValueError:
            sent = timezone.now()
        email_name = safe_filename(f"{sent:%Y-%m-%d %H%M} {subject}.eml")
        batch = MatterBatch(user, matter, source="outlook")
        results = [batch.add_stream(email_name, email.chunks())]
        for a in attachments:
            try:
                name = safe_filename(a.name)
            except ValueError:
                name = "attachment"
            results.append(batch.add_stream(name, a.chunks()))
        batch.finish()
        domain = _domain(request.data.get("sender"))
        if domain:
            hint, _ = FilingHint.objects.get_or_create(client_id=tid, sender_domain=domain, matter=matter)
            FilingHint.objects.filter(pk=hint.pk).update(count=F("count") + 1, last_used=timezone.now())
        filed = sum(1 for r in results if r.get("status") in ("uploaded", "added"))
        return Response({"matter": {"id": str(matter.pk), "name": matter.name}, "filed": filed, "results": results},
                        status=201 if filed else 200)


class WordSaveView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from core.content import replace_document_content
        user, tid = request.user, tenant_id(request.user)
        doc = request.FILES.get("document")
        if doc is None:
            return _err("The document is missing.")
        if doc.size > MAX_FILE * 2:
            return _err("The document is larger than 100 MB.")
        if request.data.get("file_id"):
            target = get_file_for_user(user, request.data["file_id"], "edit")   # 404, legal hold 403, check-out 409
            try:
                saved, version = replace_document_content(target, doc.read(), user, str(request.data.get("note") or "Saved from Word"))
            except ValueError as exc:
                return _err(str(exc))
            return Response({"file_id": saved.pk, "filename": saved.filename, "version": version})
        from core.ingest import MatterBatch
        matter = matters_for_user(user).filter(pk=request.data.get("matter_id"), client_id=tid).first() \
            if request.data.get("matter_id") else None
        if matter is None:
            return _err("Choose a document to update or a matter for the new document.", 404)
        if not (user_has_perm(user, "document.upload") or user_has_perm(user, "document.create")):
            return _err("You cannot add documents.", 403)
        try:
            name = safe_filename(str(request.data.get("filename") or doc.name or "Document.docx"))
        except ValueError:
            name = "Document.docx"
        if not name.lower().endswith(".docx"):
            name += ".docx"
        batch = MatterBatch(user, matter, source="word")
        result = batch.add_stream(name, doc.chunks())
        batch.finish()
        if result.get("status") not in ("uploaded", "added", "duplicate"):
            return _err(result.get("message") or "The document could not be saved.")
        return Response({"file_id": result.get("file_id"), "filename": name, "status": result.get("status")}, status=201)


class WordClausesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from document_automation.models import Clause
        tid = tenant_id(request.user)
        if tid is None:
            return Response({"results": []})
        names = set()
        for username, email in get_user_model().objects.filter(client_id=tid).values_list("username", "email"):
            names.update(n for n in (username, email) if n)
        qs = Clause.objects.filter(is_active=True, client_name__in=sorted(names))
        term = str(request.query_params.get("search") or "").strip()[:100]
        if term:
            qs = qs.filter(Q(title__icontains=term) | Q(content__icontains=term))
        return Response({"results": [{"id": str(c.pk), "title": c.title, "content": c.content[:20000],
                                      "category": c.category.name if c.category_id else ""}
                                     for c in qs.select_related("category").order_by("title")[:50]]})
