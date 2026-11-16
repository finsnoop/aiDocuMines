from celery import shared_task
from django.core.mail import send_mail
from django.utils.timezone import now
from .models import CustomUser, PasswordResetToken
from .utils import send_signup_email, send_password_reset_email
from .utils import EMAIL_FROM
from django.template.loader import render_to_string
from django.core.mail import send_mail, EmailMultiAlternatives

@shared_task
def send_signup_email_task(user_id):
    """Celery task to send a signup confirmation email asynchronously."""
    try:
        user = CustomUser.objects.get(id=user_id)
        send_signup_email(user)
        return f"Signup email sent to {user.email}"
    except CustomUser.DoesNotExist:
        return f"User with ID {user_id} not found"



@shared_task
def send_password_reset_email_task(user_id, token_str):
    """Celery task to send a password reset email asynchronously."""
    try:
        user = CustomUser.objects.get(id=user_id)
        reset_token = PasswordResetToken.objects.get(token=token_str)  # Fix: Query by `token`, not `id`
        send_password_reset_email(user, reset_token)
        return f"Password reset email sent to {user.email}"
    except CustomUser.DoesNotExist:
        return f"User not found (User ID: {user_id})"
    except PasswordResetToken.DoesNotExist:
        return f"Password reset token not found (Token: {token_str})"

@shared_task
def cleanup_expired_tokens():
    """Scheduled Celery task to delete expired password reset tokens."""
    expired_tokens = PasswordResetToken.objects.filter(
        created_at__lt=now() - PasswordResetToken.EXPIRATION_TIME, is_used=False
    )
    count = expired_tokens.count()
    expired_tokens.delete()
    return f"Deleted {count} expired password reset tokens"


@shared_task
def send_admin_password_reset_email_task(user_id):
    try:
        user = CustomUser.objects.get(id=user_id)
        send_signup_email(user, subject="Set a new aiDocuMines password")
    except CustomUser.DoesNotExist:
        return "User no longer exists"


@shared_task
def send_password_reset_success_email_task(user_email):
    """Sends confirmation email after a successful password reset."""
    from django.template.loader import render_to_string
    from django.core.mail import EmailMultiAlternatives
    from .models import CustomUser

    subject = "Your aiDocuMines Password Was Reset"
    from_email = EMAIL_FROM
    to_email = [user_email]

    try:
        user = CustomUser.objects.get(email=user_email)
    except CustomUser.DoesNotExist:
        return f"User with email {user_email} not found"

    # Prepare HTML and plain fallback
    context = {"user": user}
    html_content = render_to_string("email/password_reset_success.html", context)
    text_content = (
        f"Dear {user_email},\n\n"
        f"Your password has been successfully reset.\n"
        f"If you did not initiate this, please contact us immediately at support@aidocumines.com\n\n"
        f"Best regards,\n"
        f"aiDocuMines Support Team"
    )

    msg = EmailMultiAlternatives(subject, text_content, from_email, to_email)
    msg.attach_alternative(html_content, "text/html")
    msg.send(fail_silently=False)
    return f"Password reset confirmation email sent to {user_email}"



@shared_task(time_limit=30, soft_time_limit=20)
def send_signin_code_task(user_id, code):
    from django.conf import settings
    user = CustomUser.objects.get(pk=user_id)
    send_mail("Your Verification Code", f"Your verification code is: {code}. It expires in 5 minutes.",
              settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
