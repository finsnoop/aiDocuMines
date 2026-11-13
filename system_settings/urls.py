from django.urls import path
from .views import (
    SystemSettingsView,
    SystemSettingsResetView,
    SystemSettingsAuditTrailView,
    SystemSettingsSchemaView,
    PlatformGlobalSettingsView,
    PlatformGlobalSettingsPublicView,
)

urlpatterns = [
    path("auth/admin/system-settings/", SystemSettingsView.as_view(), name="system_settings_get"),
    path("auth/admin/system-settings/update/", SystemSettingsView.as_view(), name="system_settings_post"),
    path("auth/admin/system-settings/reset/", SystemSettingsResetView.as_view(), name="system_settings_reset"),
    path("auth/admin/system-settings/audit/", SystemSettingsAuditTrailView.as_view(), name="system_settings_audit"),
    path("auth/admin/system-settings/schema/", SystemSettingsSchemaView.as_view(), name="system_settings_schema"),
    # Platform-wide global settings (superadmin only)
    path("auth/admin/platform-settings/", PlatformGlobalSettingsView.as_view(), name="platform_settings"),
    # Public read-only endpoint for frontend bootstrap
    path("platform-settings/public/", PlatformGlobalSettingsPublicView.as_view(), name="platform_settings_public"),
]
