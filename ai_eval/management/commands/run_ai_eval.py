"""Run the AI evaluation suite now and print the scores.

    docker exec aidocumines_web python manage.py run_ai_eval --as-user admin@example.com --suite all [--limit 3]

Model calls are made as that person (their organisation's configured model; their AI limits and metering apply).
"""
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ai_eval.models import EvalRun
from ai_eval.runner import SUITES, execute


class Command(BaseCommand):
    help = "Run the AI evaluation suite against the configured model."

    def add_arguments(self, parser):
        parser.add_argument("--as-user", required=True)
        parser.add_argument("--suite", default="all", choices=["all", *SUITES])
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **opts):
        user = get_user_model().objects.filter(email__iexact=opts["as_user"], is_active=True).first()
        if user is None:
            raise CommandError("No active user with that email.")
        from django.utils import timezone
        for suite in (SUITES if opts["suite"] == "all" else [opts["suite"]]):
            run = EvalRun.objects.create(suite=suite, case_limit=opts["limit"], started_by=user, status="running")
            try:
                execute(run)
                run.status = "done"
            except Exception as exc:
                run.status, run.error = "failed", str(exc)[:1000]
            run.finished_at = timezone.now()
            run.save()
            headline = {k: v for k, v in run.metrics.items() if k not in ("per_case", "fields", "by_topic")}
            self.stdout.write(f"{suite}: {run.status} {json.dumps(headline)}")
