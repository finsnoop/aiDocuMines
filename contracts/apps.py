from django.apps import AppConfig


class ContractsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "contracts"
    verbose_name = "Contract lifecycle"

    def ready(self):
        from . import esign, lifecycle, playbook  # noqa: F401  registers the daily, review and e-signature tasks
