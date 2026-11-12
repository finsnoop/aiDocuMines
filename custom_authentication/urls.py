from django.urls import path
from .views import (
    SignupView, LoginView, LogoutView, PasswordResetRequestView, PasswordResetView,
    RefreshTokenView, UserProfileView, UpdateUserProfileView, ChangePasswordView,
    DeactivateAccountView, DeleteAccountView, ListUsersView, DisableUserView,
    EnableUserView, ListUserAPIKeysView, RevokeAPIKeyView, AdminResetUserPasswordView,
    PromoteUserToAdminView, DemoteUserFromAdminView, UserActivityLogView, CreateAdminUserView, CreateUserView,
    WhoAmIView, LogoutAllView, RetrieveUserView, RetrieveClientSecretsView, RetrieveClientView, SupersuperuserResetView, SupersuperuserCleanDatabaseView, TwoFASetupView, TwoFAVerifyView, TwoFADisableView, TwoFAStatusView, TwoFARotateSecretView,
    # Role Management
    ListRolesView, GetUserRolesView, AssignUserRoleView, RemoveUserRoleView, SetUserRolesView
)

urlpatterns = [
    # 🔐 AUTHENTICATION ENDPOINTS
    path("signup/", SignupView.as_view(), name="auth_signup"),
    path("login/", LoginView.as_view(), name="auth_login"),
    path("logout/", LogoutView.as_view(), name="auth_logout"),
    path("token/refresh/", RefreshTokenView.as_view(), name="auth_token_refresh"),

    # 🔑 PASSWORD RECOVERY
    path("password/request-reset/", PasswordResetRequestView.as_view(), name="auth_password_request_reset"),
    path("password/reset/", PasswordResetView.as_view(), name="auth_password_reset"),
    path("password/change/", ChangePasswordView.as_view(), name="auth_password_change"),

    # 👤 USER SELF-MANAGEMENT
    path("user/profile/", UserProfileView.as_view(), name="user_profile"),
    path("user/profile/update/", UpdateUserProfileView.as_view(), name="user_profile_update"),
    path("user/account/deactivate/", DeactivateAccountView.as_view(), name="user_account_deactivate"),
    path("user/account/delete/", DeleteAccountView.as_view(), name="user_account_delete"),

    # 🛠️ API KEY MANAGEMENT
    path("user/api-keys/", ListUserAPIKeysView.as_view(), name="user_api_keys_list"),
    path("user/api-keys/revoke/", RevokeAPIKeyView.as_view(), name="user_api_keys_revoke"),

    # 👑 SUPERUSER / ADMIN BOOTSTRAP
    path("admin/initialize/", CreateAdminUserView.as_view(), name="admin_initialize_first_user"),

    # 👮‍♂️ ADMIN USER MANAGEMENT
    path("admin/users/", ListUsersView.as_view(), name="admin_users_list"),
    path("admin/users/create/", CreateUserView.as_view(), name="admin_users_create"),
    path("admin/users/<int:user_id>/", RetrieveUserView.as_view(), name="admin_users_retrieve"),
    path("admin/users/<int:user_id>/disable/", DisableUserView.as_view(), name="admin_users_disable"),
    path("admin/users/<int:user_id>/enable/", EnableUserView.as_view(), name="admin_users_enable"),
    path("admin/users/<int:user_id>/activity/", UserActivityLogView.as_view(), name="admin_users_activity"),
    path("admin/users/<int:user_id>/oauth-credentials/", RetrieveClientSecretsView.as_view(), name="admin_users_oauth_credentials"),
    path("admin/users/<int:user_id>/reset-password/", AdminResetUserPasswordView.as_view(), name="admin_users_reset_password"),
    path("admin/users/<int:user_id>/promote/", PromoteUserToAdminView.as_view(), name="admin_users_promote"),
    path("admin/users/<int:user_id>/demote/", DemoteUserFromAdminView.as_view(), name="admin_users_demote"),

    # 🎭 ROLE MANAGEMENT
    path("admin/roles/", ListRolesView.as_view(), name="admin_roles_list"),
    path("admin/users/<int:user_id>/roles/", GetUserRolesView.as_view(), name="admin_users_roles"),
    path("admin/users/<int:user_id>/roles/assign/", AssignUserRoleView.as_view(), name="admin_users_roles_assign"),
    path("admin/users/<int:user_id>/roles/remove/", RemoveUserRoleView.as_view(), name="admin_users_roles_remove"),
    path("admin/users/<int:user_id>/roles/set/", SetUserRolesView.as_view(), name="admin_users_roles_set"),

    # 🏢 CLIENT (TENANT) MANAGEMENT
    path("admin/clients/<int:client_id>/", RetrieveClientView.as_view(), name="admin_clients_retrieve"),

    # 👑 SUPERUSER / ADMIN BOOTSTRAP
    path("admin/initialize/", CreateAdminUserView.as_view(), name="admin_initialize_first_user"),
    path("admin/supersuperuser-reset/", SupersuperuserResetView.as_view(), name="admin_supersuperuser_reset"),
    
    # SUPERSUPER USER DB CLEANING 
    path("admin/supersuperuser-clean-db/", SupersuperuserCleanDatabaseView.as_view(), name="admin_supersuperuser_clean_db"),

    # 2FA
    path("2fa/setup/", TwoFASetupView.as_view(), name="2fa_setup"),
    path("2fa/verify/", TwoFAVerifyView.as_view(), name="2fa_verify"),
    path("2fa/status/", TwoFAStatusView.as_view(), name="2fa_status"),
    path("2fa/disable/", TwoFADisableView.as_view(), name="2fa_disable"),
    path("2fa/rotate/", TwoFARotateSecretView.as_view(), name="2fa_rotate"),
    path("whoami/", WhoAmIView.as_view(), name="auth_whoami"),
    path("logout/all/", LogoutAllView.as_view(), name="auth_logout_all"),
]

