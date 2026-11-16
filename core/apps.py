from django.apps import AppConfig
from django.conf import settings
from elasticsearch_dsl import connections

class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        """
        Called automatically when Django loads the app.
        Creates the default Elasticsearch connection using settings
        and loads signals for indexing documents.
        """
        from core.ai_cache import configure_ai_cache
        configure_ai_cache()

        es_settings = settings.ELASTICSEARCH_DSL.get('default', {})

        try:
            # Will raise KeyError if alias does not exist
            connections.get_connection('default')
        except KeyError:
            connections.create_connection(
                alias='default',
                hosts=[es_settings.get('hosts')],
                http_auth=es_settings.get('http_auth'),
                timeout=30,
            )

        # Import signal handlers so they are registered when Django boots
        import core.signals

        # Register bulk processing handlers
        try:
            from core.bulk_registry import discover_and_register
            discover_and_register()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"Bulk handler registration failed: {exc}")

