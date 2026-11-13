# grid_documents_interrogation/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .db_meta_views import test_db_connection, list_tables, list_columns

from .views import TopicViewSet, QueryViewSet, DatabaseConnectionViewSet
from .views_models import AvailableModelsView, ProviderModelsView

router = DefaultRouter()
router.register(r"topics", TopicViewSet, basename="topics")
router.register(r"queries", QueryViewSet, basename="queries")
router.register(r"connections", DatabaseConnectionViewSet, basename="db-connections")

urlpatterns = [
    # 🧠 MAIN INTERROGATION ROUTES
    path("", include(router.urls)),

    # Model discovery (LiteLLM, Ollama, vLLM, OpenAI)
    path("proxy/models/", ProviderModelsView.as_view(), name="provider_models"),
    path("models/available/", AvailableModelsView.as_view(), name="available_models"),

    # 🧪 DATABASE UTILITIES
    path("db/test-connection/", test_db_connection, name="db_test_connection"),
    path("db/tables/", list_tables, name="db_list_tables"),
    path("db/columns/", list_columns, name="db_list_columns"),
]

