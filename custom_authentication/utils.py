import os
import secrets
import string
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.timezone import now
from oauth2_provider.models import Application
from .models import CustomUser, PasswordResetToken, UserActivityLog


# Email Configuration
EMAIL_FROM = "no-reply@aidocumines.com"


def log_activity(user, event, metadata=None):
    """Logs user activity with optional metadata."""
    UserActivityLog.objects.create(user=user, event=event, metadata=metadata or {})


def generate_secure_password(length=12):
    """Generates a strong secure temporary password."""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in password)
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
                and any(c in string.punctuation for c in password)):
            return password


from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from .models import UserActivityLog
from oauth2_provider.models import Application

EMAIL_FROM = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@example.com")


def send_signup_email(user, subject="Welcome to aiDocuMines"):
    """Send a one-use password-setting link, never passwords or OAuth secrets."""
    from django.conf import settings
    from urllib.parse import urlencode
    token = PasswordResetToken.objects.create(user=user)
    url = settings.FRONTEND_URL.rstrip("/") + "/set-password?" + urlencode({"token": str(token.token)})
    send_mail(subject, f"Set your password using this link (valid for one hour):\n\n{url}",
              EMAIL_FROM, [user.email], fail_silently=False)


def send_password_reset_email(user, reset_token):
    """Sends a password reset email with a unique token."""
    subject = "Password Reset Request"
    message = (
        f"Dear {user.email},\n\n"
        f"We received a request to reset your password. Use the token below to proceed:\n\n"
        f"Reset Token: {reset_token.token}\n"
        f"This token is valid for 1 hour.\n\n"
        f"If you did not request a password reset, please ignore this email.\n\n"
        f"Best Regards,\n"
        f"aiDocuMines Support Team"
    )
    send_mail(subject, message, EMAIL_FROM, [user.email], fail_silently=False)


def get_client_credentials(client_id):
    """Retrieves user credentials using a client_id."""
    try:
        application = Application.objects.get(client_id=client_id)
        user = application.user
        return {
            "client_id": user.client_id,
            "client_secret": user.client_secret_plain
        }
    except Application.DoesNotExist:
        return None


def validate_reset_token(token):
    """Validates whether a password reset token is still valid."""
    try:
        token_obj = PasswordResetToken.objects.get(token=token)
        if token_obj.is_valid():
            return token_obj
    except PasswordResetToken.DoesNotExist:
        return None
    return None

