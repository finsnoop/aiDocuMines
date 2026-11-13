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
from .utils import STATE_REGISTRY, NONCE_REGISTRY  # Keep for reference if needed in future views

app_name = "integrations"

urlpatterns = [
    path("endpoint-requests/", ConnectorEndpointRequestView.as_view(), name="connector-endpoint-requests"),
    # Generic Connector API (used by frontend connectors page)
    path("<str:service>/auth/initiate/", ConnectorAuthInitiateView.as_view(), name="connector-auth-initiate"),
    path("<str:service>/auth/status/", ConnectorAuthStatusView.as_view(), name="connector-auth-status"),
    path("<str:service>/auth/revoke/", ConnectorAuthRevokeView.as_view(), name="connector-auth-revoke"),
    path("<str:service>/connect/", ConnectorConnectView.as_view(), name="connector-connect"),
    path("<str:service>/files/", ConnectorFilesView.as_view(), name="connector-files"),
    path("<str:service>/import/", ConnectorImportView.as_view(), name="connector-import"),
    path("<str:service>/export/", ConnectorExportView.as_view(), name="connector-export"),

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

