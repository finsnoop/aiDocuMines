# integrations/urls.py

from django.urls import path
from .views import (
    NextcloudAutologinView,
    NextcloudRedirectView,
    IntegrationLogListView,
    SyncUserToNextcloudView,
    ConnectorAuthInitiateView,
    ConnectorAuthStatusView,
    ConnectorAuthRevokeView,
    ConnectorConnectView,
    ConnectorFilesView,
    ConnectorImportView,
    ConnectorExportView,
    ConnectorEndpointRequestView,
    CustomOIDCMetadataView,
)
from .views_oidc import OIDCCallbackView
from . import views_connectors as vc
from .utils import STATE_REGISTRY, NONCE_REGISTRY  # Keep for reference if needed in future views

app_name = "integrations"

urlpatterns = [
    path("endpoint-requests/", ConnectorEndpointRequestView.as_view(), name="connector-endpoint-requests"),
    # Connector API (Integrations page). Nextcloud and OnlyOffice are delegated to their own views.
    path("catalog/", vc.CatalogView.as_view(), name="connector-catalog"),
    path("jobs/<int:job_id>/", vc.JobView.as_view(), name="connector-job"),
    path("hooks/<str:token>/upload/", vc.InboundUploadView.as_view(), name="connector-inbound-upload"),
    path("<str:service>/auth/initiate/", vc.InitiateView.as_view(), name="connector-auth-initiate"),
    path("<str:service>/auth/callback/", vc.CallbackView.as_view(), name="connector-auth-callback"),
    path("<str:service>/auth/status/", vc.StatusView.as_view(), name="connector-auth-status"),
    path("<str:service>/auth/revoke/", vc.RevokeView.as_view(), name="connector-auth-revoke"),
    path("<str:service>/connect/", vc.ConnectView.as_view(), name="connector-connect"),
    path("<str:service>/test/", vc.TestEventView.as_view(), name="connector-test-event"),
    path("<str:service>/files/", vc.FilesView.as_view(), name="connector-files"),
    path("<str:service>/import/", vc.ImportView.as_view(), name="connector-import"),
    path("<str:service>/export/", vc.ExportView.as_view(), name="connector-export"),

    # API: Trigger autologin or async provisioning for current user
    path("nextcloud-autologin/", NextcloudAutologinView.as_view(), name="nextcloud-autologin"),

    # Web redirect: For direct browser-based login
    path("nextcloud-redirect/", NextcloudRedirectView.as_view(), name="nextcloud-redirect"),

    # Admin: Trigger manual user provisioning for a specific user
    path("sync-user/", SyncUserToNextcloudView.as_view(), name="sync-user"),

    # Admin: List integration logs
    path("logs/", IntegrationLogListView.as_view(), name="integration-log-list"),

    # OpenID Connect Metadata endpoint
    path(".well-known/openid-configuration", CustomOIDCMetadataView.as_view(), name="oidc-metadata"),

    # OpenID Connect Callback handler
    path("oidc/callback/", OIDCCallbackView.as_view(), name="oidc-callback"),
]

