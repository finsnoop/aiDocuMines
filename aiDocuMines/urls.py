from django.contrib import admin
from django.urls import path, include, re_path
from drf_yasg.views import get_schema_view as swagger_get_schema_view
from drf_yasg import openapi
from oauth2_provider.views import TokenView, RevokeTokenView
from django.http import JsonResponse
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.conf import settings
from rest_framework.permissions import AllowAny
from integrations.views import CustomOIDCMetadataView



# Swagger schema configuration
schema_view = swagger_get_schema_view(
    openapi.Info(
        title="aiDocuMines API Documentation",
        default_version="v1",
        description="API documentation for the Document Conversion platform",
        terms_of_service="https://www.aidocumines.com/terms/",
        contact=openapi.Contact(email="tmf_mindfactory@exigent-group.com"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=(AllowAny,),
)

# Health check endpoint
def health_check(request):
    return JsonResponse({"status": "OK", "message": "Service is running!"}, status=200)

# Root URL response
def root_response(request):
    return JsonResponse(
        {
            "message": "Welcome to the aiDocuMines API",
            "docs": "/api/v1/swagger/",
            "redoc": "/api/v1/redoc/",
            "health_check": "/api/v1/health/",
            "auth_token": "/o/token/",
        }
    )


urlpatterns = [
    path("", root_response, name="root"),
    path("api/v1/health/", health_check, name="health-check"),
    
    # Admin panel
    path("admin/", admin.site.urls),
    
    # Include core app routes
    path("api/v1/core/", include("core.urls")),
    
    # Include document translation app routes
    path("api/v1/translation/", include("document_translation.urls")),

    # Include document OCR app routes ✅
    path("api/v1/ocr/", include("document_ocr.urls")),
    
    # Include document anonymizer app routes
    path("api/v1/anonymizer/", include("document_anonymizer.urls")),
   
    # Include file monitoring app routes
    path("api/v1/file-monitor/", include("file_monitor.urls")),

    # ✅ Include file system app routes
    path("api/v1/file-system/", include("file_system.urls")),

    # Include authentication app routes
    path("api/v1/auth/", include("custom_authentication.urls")),

    # Include core app routes
    path("api/v1/grid-documents-interrogation/", include("grid_documents_interrogation.urls")),

    # Include document operations app routes
    path("api/v1/documents/", include("document_operations.urls")),

    # 🔍 NEW: semantic search endpoints
    path("api/v1/document-search/", include("document_search.urls")),

    # Document Structure
    path("api/v1/document-structures/", include("document_structures.urls")),   # ✅ ADD THIS LINE

    # Document Elasticsearch
    path("api/v1/es/", include("file_elasticsearch.urls")),

    # Platform Data Insights
    path("api/v1/insights/", include("platform_data_insights.urls")),

    # Swagger API Documentation URLs
    re_path(r"^api/v1/swagger(?P<format>\.json|\.yaml)$", schema_view.without_ui(cache_timeout=0), name="schema-json"),
    path("api/v1/swagger/", schema_view.with_ui("swagger", cache_timeout=0), name="schema-swagger-ui"),
    path("api/v1/redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),

    # 🔥 Add this line FIRST
    path("o/.well-known/openid-configuration", CustomOIDCMetadataView.as_view(), name="custom-oidc-metadata"),

    # OAuth2 Endpoints
    path("o/", include("oauth2_provider.urls", namespace="oauth2_provider")),
    path("o/token/", TokenView.as_view(), name="token"),
    path("o/revoke-token/", RevokeTokenView.as_view(), name="revoke-token"),

    # System Settings
    path("api/v1/system-settings/", include("system_settings.urls")),
    
    # Integrations
    path("api/v1/integrations/", include("integrations.urls", namespace="integrations")),

    # Cost Centre
    path("api/v1/cost/", include(("cost_centre.urls", "cost_centre"), namespace="cost")),

    # Home Dashboard Analytics
    path("api/v1/home-analytics/", include("home_dash_analytics.urls")),

    # Email Management
    path("api/v1/email/", include("email_management.urls")),

    # Legal Vertical APIs
    path('api/v1/private-equity/', include('private_equity.urls')),
    path('api/v1/class-actions/', include('class_actions.urls')),
    path('api/v1/labor-employment/', include('labor_employment.urls')),
    path('api/v1/ip-litigation/', include('ip_litigation.urls')),
    path('api/v1/regulatory-compliance/', include('regulatory_compliance.urls')),

    # Public website enquiries (demo requests, contact form)
    path('api/v1/enquiries/', include('enquiries.urls')),

    # Service Analytics
    path('api/v1/service-analytics/', include('service_analytics.urls')),

    # Document Classification/Clustering
    path('api/v1/classification/', include('document_classification.urls')),

    # New Document Services - May 2025
    path('api/v1/redlining/', include('document_redlining.urls')),
    path('api/v1/workflows/', include('document_workflows.urls')),
    path('api/v1/automation/', include('document_automation.urls')),
    path('api/v1/versioning/', include('document_versioning.urls')),

]

# Serve static files for Swagger UI
urlpatterns += staticfiles_urlpatterns()
