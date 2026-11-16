from django.apps import AppConfig


class DocumentOperationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'document_operations'

    def ready(self):
        import document_operations.signals  # Ensures signal is registered
