from django.apps import AppConfig


class AiEvalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ai_eval"
    verbose_name = "AI evaluation"

    def ready(self):
        from . import runner  # noqa: F401  registers the evaluation task
