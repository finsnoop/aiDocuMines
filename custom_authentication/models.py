from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
import uuid
from django.utils.timezone import now, timedelta
from django.utils.crypto import get_random_string
from oauth2_provider.models import Application
from django.utils.timezone import now
from django.contrib.auth.models import AbstractUser, BaseUserManager, Group
from django.conf import settings
from django.db import models
import pyotp

User = settings.AUTH_USER_MODEL

class CustomUserManager(BaseUserManager):
    """Custom manager for user creation."""
    
    def create_user(self, email, client, password=None, **extra_fields):
        """Create a standard user with email as the unique identifier."""
        if not email:
            raise ValueError("The Email field must be set")
        if not client:
            raise ValueError("Client name is required")
        
        email = self.normalize_email(email)
        extra_fields.setdefault("is_active", True)
        user = self.model(email=email, client=client, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, client, password=None, **extra_fields):
        """Create a superuser with elevated permissions."""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, client, password, **extra_fields)


class Client(models.Model):
    name = models.CharField(max_length=255, unique=True)
    address = models.TextField(blank=True, null=True)
    industry = models.CharField(max_length=255, blank=True)
    use_case = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Document(models.Model):
    title = models.CharField(max_length=255)
    content = models.TextField()

    def __str__(self):
        return self.title
    

class UserAPICall(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_calls")  # Add related_name here
    endpoint = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} - {self.endpoint} at {self.timestamp}"


class CustomUser(AbstractUser):
    """Custom User model with extended fields and OAuth2 credentials."""
    
    username = models.CharField(max_length=150, unique=True, blank=True, null=True)  # Make username optional
    email = models.EmailField(unique=True)
    # organisation = models.CharField(max_length=255)

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="users", null=True, blank=True)

    is_2fa_enabled = models.BooleanField(default=False)
    totp_secret = models.CharField(max_length=32, blank=True, null=True)
    is_2fa_verified = models.BooleanField(default=False)  # Optional: flag after successful verification

    preferences = models.JSONField(default=dict, blank=True, null=True)
    
    # Additional fields
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    contact_phone = models.CharField(max_length=50, blank=True, null=True)
    contact_email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    industry = models.CharField(max_length=255, blank=True, null=True)
    use_case = models.TextField(blank=True, null=True)

    # OAuth2 credentials
    # client_id = models.CharField(max_length=255, unique=True, blank=True, null=True)
    client_secret_plain = models.CharField(max_length=255, blank=True)
    client_secret_hashed = models.CharField(max_length=255, unique=True, blank=True)
    temp_password = models.CharField(max_length=128, blank=True)

    # New fields for tracking user activity and profile information
    profile_created_at = models.DateTimeField(default=now)
    last_login = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(null=True, blank=True)
    total_time_logged_in = models.DurationField(default=timedelta(0))
    total_api_calls_made = models.PositiveIntegerField(default=0)
    account_status = models.CharField(max_length=50, choices=[('active', 'Active'), ('inactive', 'Inactive')], default='active')
    subscription_plan = models.CharField(max_length=50, choices=[('free', 'Free'), ('premium', 'Premium')], default='free')
    plan_expiry_date = models.DateTimeField(null=True, blank=True)
    two_factor_enabled = models.BooleanField(default=False)
    # roles = models.ManyToManyField('auth.Group', related_name="custom_auth_users", blank=True)  # Assuming you use Groups as roles
    
    # Fix the conflicting related_name values
    roles = models.ManyToManyField('auth.Group', related_name='custom_auth_roles', blank=True)  # Changed related_name
    groups = models.ManyToManyField('auth.Group', related_name='custom_auth_groups', blank=True)  # Changed related_name
    user_permissions = models.ManyToManyField('auth.Permission', related_name='custom_auth_users_permissions', blank=True)
    
    last_document_edited = models.ForeignKey('Document', null=True, blank=True, on_delete=models.SET_NULL)
    user_preferences = models.JSONField(blank=True, null=True)
    notifications_enabled = models.BooleanField(default=True)

    groups = models.ManyToManyField(
        "auth.Group",
        related_name="custom_auth_users",  
        blank=True,
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        related_name="custom_auth_users_permissions",  
        blank=True,
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["client"]

    objects = CustomUserManager()

    def save(self, *args, **kwargs):
        """Save the user first, then create OAuth2 credentials."""
        
        is_new = self._state.adding  # Check if this is a new user
        super().save(*args, **kwargs)  # Save user first
        
        if is_new:  # Only generate credentials if the user is new
            #app_name = f"{self.organisation.lower()}_app"
            app_name = f"{self.client.name.lower()}_app" if self.client and self.client.name else "default_app"

            application = Application.objects.create(
                name=app_name,
                user=self,  
                client_type=Application.CLIENT_CONFIDENTIAL,
                authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS
            )

            # self.client_id = application.client_id
            self.client_secret_plain = application.client_secret  
            self.client_secret_hashed = application.client_secret  

            super().save(update_fields=["client_id", "client_secret_plain", "client_secret_hashed"])

    def get_client_credentials(self):
        """Returns client_id and the plain client_secret before hashing."""
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret_plain
        }

    def __str__(self):
        return f"{self.email} ({self.client.name if self.client else 'No Client'})"


class PasswordResetToken(models.Model):
    """Model to store password reset tokens with expiration handling."""
    
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(default=now)
    is_used = models.BooleanField(default=False)
    
    EXPIRATION_TIME = timedelta(hours=1)  # Token valid for 1 hour

    def is_valid(self):
        """Check if the token is still valid."""
        return (now() - self.created_at) < self.EXPIRATION_TIME and not self.is_used

    def __str__(self):
        return f"{self.user.email} - {self.token}"


class APIKey(models.Model):
    """Tracks OAuth2 applications (API keys) created for a user."""
    
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="api_keys")
    client_id = models.CharField(max_length=255, unique=True)
    client_secret_hashed = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"API Key for {self.user.email} - {self.client_id}"


class AccountDeletionRequest(models.Model):
    """Stores requests from users to permanently delete their accounts."""
    
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="deletion_request")
    requested_at = models.DateTimeField(auto_now_add=True)
    is_processed = models.BooleanField(default=False)  # Admin can mark it as processed

    def __str__(self):
        return f"Deletion Request - {self.user.email} ({'Processed' if self.is_processed else 'Pending'})"



class UserActivityLog(models.Model):
    """Logs user actions such as login, logout, password changes, API key actions."""
    
    EVENT_CHOICES = [
        ("LOGIN", "User logged in"),
        ("LOGOUT", "User logged out"),
        ("PASSWORD_RESET", "Password reset"),
        ("API_KEY_CREATED", "API Key created"),
        ("API_KEY_REVOKED", "API Key revoked"),
        ("ACCOUNT_DEACTIVATED", "Account deactivated"),
        ("ACCOUNT_REACTIVATED", "Account reactivated"),
        ("ACCOUNT_DELETION_REQUESTED", "Account deletion requested"),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="activity_logs")
    event = models.CharField(max_length=50, choices=EVENT_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(blank=True, null=True)  # Optional additional details

    def __str__(self):
        return f"[{self.timestamp}] {self.user.email} - {self.get_event_display()}"


class AdminActionLog(models.Model):
    """Tracks administrative actions like disabling/enabling users and password resets."""
    
    admin_user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="admin_actions")
    target_user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="affected_by_admin")
    action = models.CharField(
        max_length=50,
        choices=[
            ("DISABLE_ACCOUNT", "Disabled user account"),
            ("ENABLE_ACCOUNT", "Enabled user account"),
            ("RESET_PASSWORD", "Reset user password"),
        ],
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"[{self.timestamp}] {self.admin_user.email} -> {self.target_user.email} - {self.get_action_display()}"


class RefreshToken(models.Model):
    """Stores refresh tokens for OAuth2 authentication."""
    
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="refresh_tokens")
    token = models.CharField(max_length=255, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        """Checks if the refresh token is still valid."""
        return now() < self.expires_at

    def __str__(self):
        return f"Refresh Token for {self.user.email} - Expires at {self.expires_at}"


class AccountDeactivationRequest(models.Model):
    """Tracks deactivation requests before finalizing."""
    
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name="deactivation_request")
    requested_at = models.DateTimeField(auto_now_add=True)
    is_processed = models.BooleanField(default=False)

    def __str__(self):
        return f"Deactivation Request - {self.user.email} ({'Processed' if self.is_processed else 'Pending'})"


class TwoFactorCode(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="two_factor_codes")
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    EXPIRATION_MINUTES = 5

    def is_valid(self):
        return not self.is_used and (now() - self.created_at).total_seconds() < self.EXPIRATION_MINUTES * 60

    def __str__(self):
        return f"2FA Code for {self.user.email} - {'Used' if self.is_used else 'Pending'}"


class OrganisationPolicy(models.Model):
    """
    Settings an organisation's administrators control, each enforced by the platform:
    uploads (allowed types, size), the context assistant (on/off, web results,
    default model) and session timeout (applied by the web app).
    """
    client = models.OneToOneField(Client, on_delete=models.CASCADE, related_name="policy")
    allowed_file_types = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Comma-separated extensions (pdf,docx,...). Empty = platform default list.")
    max_upload_mb = models.PositiveIntegerField(default=100)
    assistant_enabled = models.BooleanField(default=True)
    assistant_web_enabled = models.BooleanField(default=True)
    default_ai_model = models.CharField(max_length=120, blank=True, default="")
    session_timeout_minutes = models.PositiveIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return f"Policy for {self.client}"


class AdminAuditEvent(models.Model):
    """Every administrative change: who did what, to which object, in which organisation, from where."""
    actor = models.ForeignKey(CustomUser, null=True, on_delete=models.SET_NULL, related_name="admin_audit_events")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    action = models.CharField(max_length=60, db_index=True)
    target_type = models.CharField(max_length=40, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    target_label = models.CharField(max_length=255, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["client", "-created_at"], name="audit_client_recent")]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.target_label}"
