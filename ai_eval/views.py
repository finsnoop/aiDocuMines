"""
AI evaluation API (/api/v1/ai-eval/), platform administrators only (runs cost model calls).

GET  runs/                 recent runs with scores
POST runs/ {suite, limit?} queue a run (suite: extraction, playbook, assistant or harness) against the caller's configured model (metered to the caller)
GET  runs/<id>/            one run
GET  summary/              the latest finished run per suite as publishable Markdown
"""
from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser
from .models import EvalRun
from .runner import SUITES, run_eval_task, summary_markdown


def _out(r, full=False):
    data = {"id": str(r.id), "suite": r.suite, "suite_label": r.get_suite_display(), "dataset": r.dataset,
            "provider": r.provider, "model": r.model, "status": r.status, "error": r.error,
            "created_at": r.created_at, "finished_at": r.finished_at,
            "headline": {k: (r.metrics or {}).get(k) for k in ("accuracy", "precision", "evidence_correct",
                                                               "injection_resisted", "status_accuracy", "unverified_rate", "answer_accuracy",
                                                               "cited", "citation_correct", "abstained", "passed",
                                                               "claims_removed", "cases")}}
    if full:
        data["metrics"] = r.metrics
    return data


def _forbidden():
    return Response({"error": "Only platform administrators can run or see AI evaluations."}, status=403)


class RunsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_platform_superuser(request.user):
            return _forbidden()
        return Response({"results": [_out(r) for r in EvalRun.objects.all()[:50]], "suites": list(SUITES)})

    def post(self, request):
        if not is_platform_superuser(request.user):
            return _forbidden()
        suite = request.data.get("suite")
        if suite not in SUITES:
            return Response({"error": "suite must be one of: " + ", ".join(SUITES) + "."}, status=400)
        try:
            limit = int(request.data["limit"]) if request.data.get("limit") not in (None, "") else None
        except (TypeError, ValueError):
            return Response({"error": "limit must be a whole number."}, status=400)
        if EvalRun.objects.filter(status__in=["pending", "running"]).exists():
            return Response({"error": "An evaluation is already running."}, status=409)
        run = EvalRun.objects.create(suite=suite, case_limit=limit, started_by=request.user)
        run_eval_task.delay(str(run.pk), user_id=request.user.pk)
        return Response(_out(run), status=202)


class RunDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not is_platform_superuser(request.user):
            return _forbidden()
        r = EvalRun.objects.filter(pk=pk).first()
        return Response(_out(r, full=True)) if r else Response({"error": "Run not found."}, status=404)


class SummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not is_platform_superuser(request.user):
            return _forbidden()
        latest = [r for r in (EvalRun.objects.filter(suite=s, status="done").first() for s in SUITES) if r]
        return HttpResponse(summary_markdown(latest), content_type="text/markdown; charset=utf-8")
