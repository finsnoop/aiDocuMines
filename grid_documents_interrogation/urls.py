# grid_documents_interrogation/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .db_meta_views import test_db_connection, list_tables, list_columns

from .views import TopicViewSet, QueryViewSet, DatabaseConnectionViewSet
from .llm_models_proxy import proxy_llm_models

router = DefaultRouter()
router.register(r"topics", TopicViewSet, basename="topics")
router.register(r"queries", QueryViewSet, basename="queries")
router.register(r"connections", DatabaseConnectionViewSet, basename="db-connections")

urlpatterns = [
    # 🧠 MAIN INTERROGATION ROUTES
    path("", include(router.urls)),

    # 🔄 LLM model list proxy
    path("proxy/models/", proxy_llm_models, name="proxy_llm_models"),

    # 🧪 DATABASE UTILITIES
    path("db/test-connection/", test_db_connection, name="db_test_connection"),
    path("db/tables/", list_tables, name="db_list_tables"),
    path("db/columns/", list_columns, name="db_list_columns"),
]

