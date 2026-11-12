from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.contrib.auth import get_user_model, authenticate, login, logout
from oauth2_provider.models import AccessToken, Application
from oauthlib.common import generate_token
from django.utils.timezone import now
from datetime import timedelta
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from .models import PasswordResetToken, CustomUser, Client, TwoFactorCode
from .serializers import PasswordResetRequestSerializer, PasswordResetSerializer, CreateAdminUserSerializer, UserActivityLogSerializer
from .tasks import send_signup_email_task, send_password_reset_email_task, send_admin_password_reset_email_task # ✅ Celery tasks
from .tasks import send_password_reset_success_email_task
from integrations.tasks import provision_nextcloud_user
import logging
from rest_framework.permissions import AllowAny
from oauth2_provider.generators import generate_client_secret
from django.contrib.auth.hashers import check_password
from rest_framework.permissions import IsAdminUser
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import UserActivityLog
from django.shortcuts import get_object_or_404
from .models import CustomUser
from rest_framework import serializers
from django.utils import timezone
from django.contrib.auth.models import AbstractUser, BaseUserManager, Group
from core.models import File
from .models import UserAPICall
from oauth2_provider.models import AccessToken
import secrets
import os
import json
import uuid
import random
from .serializers import ClientApplicationSerializer

import json
import os
from django.utils.timezone import now
from oauth2_provider.models import Application
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.signals import user_logged_in
from .utils import log_activity
import pyotp
import qrcode
import io
import base64
from rest_framework.permissions import IsAuthenticated

import pyotp  # 🔐 For TOTP verification

from .permissions import (
    IsSuperUser, IsAdminUserOnly, IsRegularUser, IsGroupMember, IsManager, IsDeveloper, IsClient,
    HasAnyRole, IsClientOrAdmin, IsClientOrAdminOrSuperUser, IsAdminOrSuperUser,
    IsClientOrAdminOrManager, IsClientOrAdminOrManagerOrSuperUser, IsClientOrAdminOrDeveloper,
    IsClientOrAdminOrManagerOrDeveloper, IsClientOrSuperUser, IsClientOrManager,
    IsClientOrDeveloper, IsGuest, IsGroupMemberAny, IsStrictManager, IsActiveClient,
    require_roles, IsFirstAdminOrSuperUser, IsSuperSuperUser
)
from django.core.mail import send_mail
from django.conf import settings

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.contrib.auth import get_user_model
from django.db import connection
import os
import logging

logger = logging.getLogger(__name__)

User = get_user_model()

#EMAIL_FROM = settings.DEFAULT_FROM_EMAIL
EMAIL_FROM = "no-reply@aidocumines.com"


# Path to store OAuth secrets
SECRETS_FILE_PATH = "logs/.superuser_secrets.json"

def store_client_secret(client_id, client_secret):
    """Store client secrets before hashing."""
    secrets = {}

    if os.path.exists(SECRETS_FILE_PATH):
        with open(SECRETS_FILE_PATH, "r") as file:
            try:
                secrets = json.load(file)
            except json.JSONDecodeError:
                secrets = {}

    secrets[client_id] = client_secret  # Store client_secret before hashing

    with open(SECRETS_FILE_PATH, "w") as file:
        json.dump(secrets, file, indent=4)



# ✅ Define Swagger parameters
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID provided at signup"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
email_param = openapi.Parameter(
    "email", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="User email"
)
password_param = openapi.Parameter(
    "password", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="User password (hidden in logs)"
)
password_param = openapi.Parameter(
    "password", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="User password"
)
organisation_param = openapi.Parameter(
    "organisation", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Organisation name"
)
contact_name_param = openapi.Parameter(
    "contact_name", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Contact person name"
)
contact_phone_param = openapi.Parameter(
    "contact_phone", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Contact phone number"
)
contact_email_param = openapi.Parameter(
    "contact_email", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Alternative contact email"
)
address_param = openapi.Parameter(
    "address", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Address"
)
industry_param = openapi.Parameter(
    "industry", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Industry"
)
use_case_param = openapi.Parameter(
    "use_case", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="User's use case"
)


def get_user_from_client_id(client_id):
    """Retrieves the User associated with a given `client_id` from OAuth2 Application."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None



def _get_managed_user(request, user_id):
    """
    Multi-tenancy for admin actions: return the target user only if the acting
    admin may manage them (same organisation; platform superusers manage everyone;
    org admins can never touch platform superusers). Otherwise behave exactly as if
    the user did not exist, so existing `except DoesNotExist` handlers return 404.
    """
    from core.access import can_manage_user
    target = _get_managed_user(request, user_id)
    if target.id != request.user.id and not can_manage_user(request.user, target):
        raise CustomUser.DoesNotExist
    return target


def _can_grant_platform_roles(request):
    from core.access import is_platform_superuser
    return is_platform_superuser(request.user)


class SignupView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    # ✅ Define the request body schema
    signup_request_schema = openapi.Schema(
        type=openapi.TYPE_OBJECT,
        required=["email", "password", "organisation", "contact_name", "contact_phone"],
        properties={
            "email": openapi.Schema(type=openapi.TYPE_STRING, format="email", description="User email"),
            "password": openapi.Schema(type=openapi.TYPE_STRING, format="password", description="User password"),
            "organisation": openapi.Schema(type=openapi.TYPE_STRING, description="Organisation name"),
            "contact_name": openapi.Schema(type=openapi.TYPE_STRING, description="Contact person name"),
            "contact_phone": openapi.Schema(type=openapi.TYPE_STRING, description="Contact phone number"),
            "contact_email": openapi.Schema(type=openapi.TYPE_STRING, format="email", description="Alternative contact email"),
            "address": openapi.Schema(type=openapi.TYPE_STRING, description="Address (optional)"),
            "industry": openapi.Schema(type=openapi.TYPE_STRING, description="Industry (optional)"),
            "use_case": openapi.Schema(type=openapi.TYPE_STRING, description="User's use case (optional)"),
        },
    )

    @swagger_auto_schema(
        operation_description="Register a new user and return client credentials along with additional user metrics.",
        request_body=signup_request_schema,  # ✅ Correct request body schema
        responses={
            201: openapi.Response("User successfully registered", openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    "message": openapi.Schema(type=openapi.TYPE_STRING, description="Success message"),
                    "user_id": openapi.Schema(type=openapi.TYPE_INTEGER, description="User ID"),
                    "organisation": openapi.Schema(type=openapi.TYPE_STRING, description="Organisation name"),
                    "contact_name": openapi.Schema(type=openapi.TYPE_STRING, description="Contact person's name"),
                    "contact_phone": openapi.Schema(type=openapi.TYPE_STRING, description="Contact phone number"),
                    "industry": openapi.Schema(type=openapi.TYPE_STRING, description="Industry"),
                    "use_case": openapi.Schema(type=openapi.TYPE_STRING, description="User's use case"),
                    "client_id": openapi.Schema(type=openapi.TYPE_STRING, description="OAuth client ID"),
                    "client_secret": openapi.Schema(type=openapi.TYPE_STRING, description="OAuth client secret"),
                }
            )),
            400: openapi.Response("Bad Request"),
            500: openapi.Response("Internal Server Error"),
        },
        tags=["Authentication"],
        manual_parameters=[email_param, password_param, organisation_param, contact_name_param, contact_phone_param, contact_email_param, address_param, industry_param, use_case_param],
    )
    def post(self, request):
        data = request.data
        email = data.get("email", "").strip().lower()  # ✅ Normalize email
        password = data.get("password")
        organisation = data.get("organisation")
        contact_name = data.get("contact_name")
        contact_phone = data.get("contact_phone")
        contact_email = data.get("contact_email", email)
        address = data.get("address", "")
        industry = data.get("industry", "")
        use_case = data.get("use_case", "")

        if not email or not password or not organisation or not contact_name or not contact_phone:
            return Response({"error": "Email, password, organisation, contact_name, and contact_phone are required"},
                            status=status.HTTP_400_BAD_REQUEST)

        # ✅ Ensure email is unique
        if User.objects.filter(email=email).exists():
            return Response({"error": "Email is already registered"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Get or create the client object
        client, _ = Client.objects.get_or_create(name=organisation)

        try:
            # ✅ Create user with all fields
            user = User.objects.create_user(
                email=email,
                password=password,
                client=client,
            )
            user.contact_name = contact_name
            user.contact_phone = contact_phone
            user.contact_email = contact_email
            user.address = address
            user.industry = industry
            user.use_case = use_case
            user.profile_created_at = timezone.now()  # Store profile creation time
            user.save()

            # ✅ Generate a **secure** client secret BEFORE saving the application
            raw_client_secret = generate_client_secret()

            # ✅ Create an OAuth2 Application for the user
            application = Application.objects.create(
                user=user,
                name=f"{user.client.name} - API Access",
                client_type=Application.CLIENT_CONFIDENTIAL,
                # authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
                authorization_grant_type=Application.GRANT_PASSWORD,
                client_secret=raw_client_secret,
            )

            # ✅ Send signup email asynchronously
            send_signup_email_task.delay(
                    user.id,
                    password,
                    application.client_id,
                    raw_client_secret
            )
            # send_signup_email_task.delay(user.id,
            #    f"Your account has been created.\n\nClient ID: {application.client_id}\nClient Secret: {raw_client_secret}")

            # Assign default "Guest" role
            try:
                guest_role = Group.objects.get(name='Guest')
            except Group.DoesNotExist:
                guest_role = Group.objects.create(name='Guest')

            user.groups.add(guest_role)

            user_preferences = {"theme": "light", "language": "en"}  # Example preferences
            user.preferences = user_preferences
            user.save()

            # Fetching user metrics (these could be calculated dynamically as needed)
            user_metrics = {
                "profile_created_at": user.profile_created_at,
                "last_login": user.last_login,
                "last_activity": user.last_activity,
                "total_time_logged_in": "N/A",  # Placeholder, you'd need a method to track this
                "total_api_calls_made": user.api_calls.count(),  # Assuming an API call model exists
                "account_status": "active" if user.is_active else "inactive",
                "total_files_uploaded": user.files.count(),  # Assuming a File model is related
                "subscription_plan": "premium",  # Or dynamically based on their subscription
                "plan_expiry_date": "2025-06-06T00:00:00Z",  # Replace with actual logic
                "2fa_enabled": user.is_2fa_enabled,  # Assuming a method for 2FA
                "roles": [role.name for role in user.roles.all()],
                "last_document_edited": "document_12345.pdf",  # Placeholder
                "user_preferences": user.preferences,  # Assuming preferences exist
                "notifications_enabled": user.notifications_enabled,
            }

            return Response({
                "message": "User registered successfully",
                "user_id": user.id,
                "organisation": organisation,
                "contact_name": contact_name,
                "contact_phone": contact_phone,
                "industry": industry,
                "use_case": use_case,
                "client_id": application.client_id,
                "client_secret": raw_client_secret,
                **user_metrics  # Add the user metrics here
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"❌ Error during signup: {str(e)}")
            return Response({"error": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def create(self, validated_data):
        # Create the user using the validated data
        user = CustomUser.objects.create_user(**validated_data)

        # Get or create the "Guest" group
        guest_role, created = Group.objects.get_or_create(name='Guest')

        # Assign the "Guest" role to the new user
        user.groups.add(guest_role)
        user.save()

        return user


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_description="Authenticate user and return an OAuth2 token. If 2FA is enabled, requires a 2FA code.",
        tags=["Authentication"],
        manual_parameters=[client_id_param, client_secret_param, email_param, password_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")

        if not all([client_id, client_secret]):
            return Response({"error": "Missing Client ID or Client Secret"}, status=status.HTTP_400_BAD_REQUEST)

        email = request.data.get("email")
        password = request.data.get("password")
        twofa_code = request.data.get("2fa_code")  # 🔐 New: Optional 2FA code from user

        if not email or not password:
            return Response({"error": "Email and password are required"}, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(email=email, password=password)

        if user is None:
            return Response({"error": "Invalid email or password"}, status=status.HTTP_401_UNAUTHORIZED)

        # 🔐 If user has 2FA enabled, require a valid email-based 2FA code
        if user.is_2fa_enabled:
            if not twofa_code:
                code = str(random.randint(100000, 999999))
                TwoFactorCode.objects.filter(user=user, is_used=False).update(is_used=True)
                TwoFactorCode.objects.create(user=user, code=code)
                try:
                    send_mail(
                        subject="Your Verification Code",
                        message=f"Your verification code is: {code}\n\nThis code expires in 5 minutes.",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[user.email],
                        fail_silently=False,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send 2FA email to {user.email}: {e}")
                return Response({
                    "requires_2fa": True,
                    "message": "Verification code sent to your email"
                }, status=status.HTTP_200_OK)

            valid_code = TwoFactorCode.objects.filter(
                user=user, code=twofa_code, is_used=False
            ).first()

            if not valid_code or not valid_code.is_valid():
                return Response({"error": "Invalid or expired 2FA code"}, status=status.HTTP_401_UNAUTHORIZED)

            valid_code.is_used = True
            valid_code.save()

        # ✅ Log user login activity
        log_activity(user, "LOGIN", {"ip": request.META.get("REMOTE_ADDR")})
        user_logged_in.send(sender=user.__class__, request=request, user=user)

        # ✅ Verify OAuth2 application
        try:
            application = Application.objects.get(client_id=client_id, user=user)
        except Application.DoesNotExist:
            return Response({"error": "Invalid Client ID or Client Secret"}, status=status.HTTP_401_UNAUTHORIZED)

        # ✅ Generate OAuth2 access token
        token = AccessToken.objects.create(
            user=user,
            token=generate_token(),
            application=application,
            expires=now() + timedelta(days=1),
            scope="read write",
        )

        login(request, user)  # Django login

        # ✅ Get user roles from groups
        roles = [group.name for group in user.groups.all()]

        # ✅ Return flattened response for frontend compatibility
        return Response({
            "access_token": token.token,
            "expires": token.expires.strftime("%Y-%m-%d %H:%M:%S"),
            "client_id": client_id,
            "client_secret": client_secret,
            "id": user.id,
            "email": user.email,
            "roles": roles,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "2fa_enabled": user.is_2fa_enabled,
            "organisation": user.client.name if user.client else None,
        }, status=status.HTTP_200_OK)


class LogoutView(APIView):
    authentication_classes = [OAuth2Authentication]
    # permission_classes = [TokenHasReadWriteScope]
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Logs out the authenticated user and revokes their token.",
        tags=["Authentication"],
        manual_parameters=[client_id_param],
    )
    def post(self, request):
        if request.auth:
            # Ensure the token is deleted from the AccessToken model
            AccessToken.objects.filter(token=request.auth.token).delete()  # Delete token from AccessToken table

        # Optionally, invalidate any other session-related data
        logout(request)

        return Response({"message": "Logged out successfully"}, status=status.HTTP_200_OK)



class PasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_description="Request a password reset token.",
        tags=["Authentication"],
        manual_parameters=[client_id_param, email_param],
    )
    def post(self, request):
        email = request.data.get("email")

        if not email:
            return Response({"error": "Email is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        reset_token = PasswordResetToken.objects.create(user=user)

        send_password_reset_email_task.delay(user.id, str(reset_token.token))

        return Response({
            "message": "Password reset token sent to your email",
            "reset_token": str(reset_token.token)
        }, status=status.HTTP_200_OK)




class PasswordResetView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token")
        new_password = request.data.get("new_password")

        try:
            token_obj = PasswordResetToken.objects.get(token=token)
        except PasswordResetToken.DoesNotExist:
            return Response({"error": "Invalid reset token"}, status=status.HTTP_400_BAD_REQUEST)

        user = token_obj.user
        user.set_password(new_password)
        user.save()

        token_obj.is_used = True
        token_obj.save()

        # ✅ Notify user via email
        send_password_reset_success_email_task.delay(user.email)

        return Response({"message": "Password reset successful"}, status=status.HTTP_200_OK)

'''
class PasswordResetView(APIView):
    # authentication_classes = [OAuth2Authentication]
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("token")
        new_password = request.data.get("new_password")

        try:
            token_obj = PasswordResetToken.objects.get(token=token)
        except PasswordResetToken.DoesNotExist:
            return Response({"error": "Invalid reset token"}, status=status.HTTP_400_BAD_REQUEST)

        token_obj.user.set_password(new_password)
        token_obj.user.save()
        token_obj.is_used = True
        token_obj.save()

        return Response({"message": "Password reset successful"}, status=status.HTTP_200_OK)
'''


class RefreshTokenView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        operation_description="Refresh an expired access token using a refresh token.",
        tags=["Authentication"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        refresh_token = request.data.get("refresh_token")

        if not all([client_id, client_secret, refresh_token]):
            return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            application = Application.objects.get(client_id=client_id)
            user = application.user
            token = AccessToken.objects.get(token=refresh_token, user=user)

            if token.expires < now():
                return Response({"error": "Refresh token has expired"}, status=status.HTTP_401_UNAUTHORIZED)

            new_access_token = AccessToken.objects.create(
                user=user,
                token=generate_token(),
                application=application,
                expires=now() + timedelta(days=1),
                scope="read write",
            )

            return Response({
                "access_token": new_access_token.token,
                "expires": new_access_token.expires.strftime("%Y-%m-%d %H:%M:%S")
            }, status=status.HTTP_200_OK)

        except (Application.DoesNotExist, AccessToken.DoesNotExist):
            return Response({"error": "Invalid client credentials or refresh token"}, status=status.HTTP_401_UNAUTHORIZED)



class UserProfileView(APIView):
    authentication_classes = [OAuth2Authentication]
    # permission_classes = [TokenHasReadWriteScope]
    # permission_classes = [IsClientOrAdminOrSuperUser]
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get the authenticated user's profile information.",
        tags=["User"],
    )
    def get(self, request):
        user = request.user

        # Retrieve the role names from the user's groups (roles)
        roles = [group.name for group in user.groups.all()]

        return Response({
            "id": user.id,
            "email": user.email,
            "organisation": user.client.name if user.client else None,
            "contact_name": user.contact_name,
            "contact_phone": user.contact_phone,
            "address": user.address,
            "industry": user.industry,
            "use_case": user.use_case,
            "roles": roles
        }, status=status.HTTP_200_OK)


class UpdateUserProfileView(APIView):
    authentication_classes = [OAuth2Authentication]
    # permission_classes = [IsClientOrAdminOrSuperUser]
    permission_classes = [IsAuthenticated]


    @swagger_auto_schema(
        operation_description="Update the authenticated user's profile.",
        tags=["User"],
    )
    def put(self, request):
        user = request.user
        data = request.data

        user.contact_name = data.get("contact_name", user.contact_name)
        user.contact_phone = data.get("contact_phone", user.contact_phone)
        user.address = data.get("address", user.address)
        user.save()

        return Response({"message": "Profile updated successfully"}, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    authentication_classes = [OAuth2Authentication]
    #permission_classes = [HasAnyRole]
    permission_classes = [IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Change password for the authenticated user.",
        tags=["User"],
    )
    def post(self, request):
        user = request.user
        current_password = request.data.get("current_password")
        new_password = request.data.get("new_password")

        if not user.check_password(current_password):
            return Response({"error": "Current password is incorrect"}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()
        return Response({"message": "Password changed successfully"}, status=status.HTTP_200_OK)


#class DeactivateAccountView(APIView):
#    authentication_classes = [OAuth2Authentication]
    # permission_classes = [IsClientOrAdminOrSuperUser]
#    permission_classes = [IsAuthenticated]


#    @swagger_auto_schema(
#        operation_description="Deactivate the authenticated user's account.",
#        tags=["User"],
#    )
#    def post(self, request):
#        user = request.user
#        user.is_active = False
#        user.save()
#        return Response({"message": "Account deactivated successfully"}, status=status.HTTP_200_OK)

class DeactivateAccountView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Deactivate the authenticated user's account.",
        tags=["User"],
    )
    def post(self, request):
        user = request.user
        user.is_active = False
        user.save()

        return Response({
            "message": "Account deactivated successfully",
            "user": {
                "id": user.id,
                "email": user.email,
                "is_active": user.is_active,
                "deactivated_at": timezone.now().isoformat()
            }
        }, status=status.HTTP_200_OK)


class ListUsersView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="List all registered users. Admin only.",
        tags=["Admin"],
    )
    def get(self, request):
        if not request.user.is_staff and not request.user.is_superuser:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        # Superusers can see all users, admins can only see users from their client
        from core.access import scope_users
        users = scope_users(User.objects.all(), request.user)

        user_data = []
        for user in users:
            user_data.append({
                "id": user.id,
                "email": user.email,
                "organisation": user.client.name if user.client else None,
                "is_active": user.is_active,
                "roles": [group.name for group in user.groups.all()]  # ✅ Add roles
            })

        return Response(user_data, status=status.HTTP_200_OK)

    #def get(self, request):
    #    if not request.user.is_staff:
    #        return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        # users = User.objects.all().values("id", "email", "organisation", "is_active")
    #    users = User.objects.filter(client=request.user.client).values("id", "email", "client__name", "is_active")

    #    return Response(users, status=status.HTTP_200_OK)


class DisableUserView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Disable a user's account. Admin only.",
        tags=["Admin"],
    )
    def post(self, request, user_id):
        if not request.user.is_staff:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        # user_id = request.data.get("user_id")

        try:
            user = _get_managed_user(request, user_id)
            user.is_active = False
            user.save()

            # ✅ Log disable event
            log_activity(user, "ACCOUNT_DEACTIVATED", {"admin_id": request.user.id})

            return Response({"message": "User account disabled successfully"}, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class DeleteAccountView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsSuperUser]

    @swagger_auto_schema(
        operation_description="Permanently delete the authenticated user's account.",
        tags=["User"],
    )
    def delete(self, request):
        user = request.user
        user.delete()
        return Response({"message": "Account deleted successfully"}, status=status.HTTP_200_OK)


class EnableUserView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Enable a user's account. Admin only.",
        tags=["Admin"],
    )
    def post(self, request, user_id):
        if not request.user.is_staff:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        # user_id = request.data.get("user_id")

        try:
            user = _get_managed_user(request, user_id)
            user.is_active = True
            user.save()

            # ✅ Log enable event
            log_activity(user, "ACCOUNT_REACTIVATED", {"admin_id": request.user.id})

            return Response({"message": "User account enabled successfully"}, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)



class ListUserAPIKeysView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="List all OAuth2 API keys (applications) for the authenticated user.",
        tags=["User"],
    )
    def get(self, request):
        user = request.user
        # applications = Application.objects.filter(user=user).values("client_id", "created", "updated")
        # return Response({"applications": list(applications)}, status=status.HTTP_200_OK)
        applications = Application.objects.filter(user=user)
        serializer = ClientApplicationSerializer(applications, many=True)
        return Response({"applications": serializer.data}, status=status.HTTP_200_OK)



class RevokeAPIKeyView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        try:
            # ✅ Use request.data directly
            client_id = request.data.get("client_id")

            if not client_id:
                return Response({"error": "Client ID is required"}, status=status.HTTP_400_BAD_REQUEST)

            # ✅ Find and delete the application
            app = Application.objects.get(client_id=client_id)
            app.delete()

            return Response({"message": "API key revoked successfully"}, status=status.HTTP_200_OK)

        except Application.DoesNotExist:
            return Response({"error": "Invalid Client ID"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



class AdminResetUserPasswordView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Admin resets a user's password.",
        tags=["Admin"],
    )
    def post(self, request, user_id):
        if not request.user.is_staff:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        new_password = request.data.get("new_password")

        try:
            user = _get_managed_user(request, user_id)
            user.set_password(new_password)
            user.save()

            send_admin_password_reset_email_task.delay(user.id, new_password)

            # ✅ Log activity
            log_activity(user, "PASSWORD_RESET", {"admin_id": request.user.id})

            # ✅ Send password reset email (manual message, not token-based)
            subject = "Your password has been reset by an admin"
            message = (
                f"Dear {user.email},\n\n"
                f"An administrator has reset your password.\n\n"
                f"Your new password is: {new_password}\n\n"
                f"Please log in and change it immediately.\n\n"
                f"Best regards,\n"
                f"aiDocuMines Support Team"
            )
            send_mail(subject, message, EMAIL_FROM, [user.email], fail_silently=False)

            return Response({"message": "User password reset and email sent successfully"}, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)



'''
class AdminResetUserPasswordView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Admin resets a user's password.",
        tags=["Admin"],
    )
    def post(self, request, user_id):
        if not request.user.is_staff:
            return Response({"error": "Admin access required"}, status=status.HTTP_403_FORBIDDEN)

        # user_id = request.data.get("user_id")

        new_password = request.data.get("new_password")

        try:
            user = _get_managed_user(request, user_id)
            user.set_password(new_password)
            user.save()

            # ✅ Log password reset event
            log_activity(user, "PASSWORD_RESET", {"admin_id": request.user.id})

            return Response({"message": "User password reset successfully"}, status=status.HTTP_200_OK)

        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
'''


class PromoteUserToAdminView(APIView):
    """Allows superusers to promote a normal user to admin."""
    authentication_classes = [OAuth2Authentication]
    # permission_classes = [IsAdminUser]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Promote a user to an admin (Superuser access required).",
        tags=["Admin"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "user_id": openapi.Schema(type=openapi.TYPE_INTEGER, description="ID of the user to promote"),
            },
            required=["user_id"],
        ),
    )
    def post(self, request, user_id):
        # user_id = request.data.get("user_id")

        if not user_id:
            return Response({"error": "User ID is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = _get_managed_user(request, user_id)

            if user.is_staff or user.is_superuser:
                return Response({"message": "User is already an admin"}, status=status.HTTP_200_OK)

            # Organisation admin by default. Platform-wide superuser status can only be
            # granted by a platform superuser who explicitly asks for it.
            user.is_staff = True
            if str(request.data.get("platform_superuser", "")).lower() == "true":
                if not _can_grant_platform_roles(request):
                    return Response({"error": "Only platform superusers can grant platform superuser status"},
                                    status=status.HTTP_403_FORBIDDEN)
                user.is_superuser = True
            user.save()

            # ✅ Adjust group roles
            guest_group, _ = Group.objects.get_or_create(name="Guest")
            admin_group, _ = Group.objects.get_or_create(name="Admin")

            # ✅ Group updates
            user.groups.remove(guest_group)  # ✅ Remove "Guest"
            user.groups.add(admin_group)     # ✅ Add "Admin"

            return Response({"message": f"User {user.email} has been promoted to admin"}, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class UserActivityLogView(APIView):
    permission_classes = [IsAdminOrSuperUser]

    def get(self, request, user_id):
        """Fetch activity logs for a specific user."""
        try:
            user = _get_managed_user(request, user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        logs = UserActivityLog.objects.filter(user=user).order_by('-timestamp')

        if logs.exists():
            serializer = UserActivityLogSerializer(logs, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response({"message": "No activity logs found for this user."}, status=status.HTTP_404_NOT_FOUND)


class CreateAdminUserView(APIView):
    """Allows first-time unauthenticated admin creation, then requires authentication for others."""
    # authentication_classes = [OAuth2Authentication]
    permission_classes = [IsFirstAdminOrSuperUser]

    def post(self, request):
        """
        Bootstrap the first platform superuser (open only while none exists), or
        let an authenticated superuser create another one.

        SECURITY: responses never contain passwords, and existing credentials are
        never returned. The OAuth client secret of the NEW account is returned once,
        to its creator, because it cannot be recovered later.
        """
        superuser_file = "logs/.superuser_secrets.json"
        user_model = get_user_model()
        is_bootstrap = not user_model.objects.filter(is_superuser=True).exists()

        if not is_bootstrap and not (
            request.user and request.user.is_authenticated and request.user.is_superuser
        ):
            return Response(
                {"error": "Admin account already exists. Authentication required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CreateAdminUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        organisation = data.get("organisation") or request.data.get("organisation")
        if not organisation:
            return Response({"organisation": ["This field is required."]},
                            status=status.HTTP_400_BAD_REQUEST)
        client, _ = Client.objects.get_or_create(name=organisation)

        user = user_model.objects.create_superuser(
            email=data["email"],
            password=data["password"],
            client=client,
            contact_name=data["contact_name"],
            contact_phone=data["contact_phone"],
            address=data["address"],
            industry=data["industry"],
            use_case=data["use_case"],
        )

        raw_client_secret = Application.objects.generate_secret()
        app = Application.objects.create(
            user=user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            name="AI DocuMines API Access",
            client_secret=raw_client_secret,
        )

        if is_bootstrap:
            # Server-side record for ops scripts (reset_and_create_superuser.sh).
            os.makedirs(os.path.dirname(superuser_file), exist_ok=True)
            with open(superuser_file, "w") as f:
                json.dump({
                    "admin_email": data["email"],
                    "admin_password": data["password"],
                    "client_id": app.client_id,
                    "client_secret": raw_client_secret,
                }, f, indent=4)
            os.chmod(superuser_file, 0o600)

        return Response({
            "message": "Admin user created successfully",
            "admin_email": data["email"],
            "client_id": app.client_id,
            "client_secret": raw_client_secret,
        }, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateUserView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]  # ✅ Ensure only admins can create users

    # ✅ Define request schema
    user_creation_schema = openapi.Schema(
        type=openapi.TYPE_OBJECT,
        required=["email", "organisation", "contact_name", "contact_phone"],  # ✅ Remove password from required
        properties={
            "email": openapi.Schema(type=openapi.TYPE_STRING, format="email", description="User email"),
            "organisation": openapi.Schema(type=openapi.TYPE_STRING, description="Organisation name"),
            "contact_name": openapi.Schema(type=openapi.TYPE_STRING, description="Contact person's name"),
            "contact_phone": openapi.Schema(type=openapi.TYPE_STRING, description="Contact phone number"),
            "contact_email": openapi.Schema(type=openapi.TYPE_STRING, format="email", description="Alternative contact email"),
            "address": openapi.Schema(type=openapi.TYPE_STRING, description="Address (optional)"),
            "industry": openapi.Schema(type=openapi.TYPE_STRING, description="Industry (optional)"),
            "use_case": openapi.Schema(type=openapi.TYPE_STRING, description="User's use case (optional)"),
            "role": openapi.Schema(type=openapi.TYPE_STRING, description="User role (Admin, Manager, Guest)"),
        },
    )

    @swagger_auto_schema(
        operation_description="Create a new user (Admin only)",
        request_body=user_creation_schema,
        responses={
            201: openapi.Response("User successfully created", openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    "message": openapi.Schema(type=openapi.TYPE_STRING, description="Success message"),
                    "user_id": openapi.Schema(type=openapi.TYPE_INTEGER, description="User ID"),
                    "email": openapi.Schema(type=openapi.TYPE_STRING, format="email", description="User email"),
                    "temporary_password": openapi.Schema(type=openapi.TYPE_STRING, description="Temporary password (user must change upon login)"),
                    "organisation": openapi.Schema(type=openapi.TYPE_STRING, description="Organisation name"),
                    "role": openapi.Schema(type=openapi.TYPE_STRING, description="User role"),
                    "client_id": openapi.Schema(type=openapi.TYPE_STRING, description="OAuth client ID"),
                    "client_secret": openapi.Schema(type=openapi.TYPE_STRING, description="OAuth client secret"),
                }
            )),
            400: openapi.Response("Bad Request"),
            403: openapi.Response("Forbidden - Only Admins can create users"),
            500: openapi.Response("Internal Server Error"),
        },
        tags=["User Management"],
    )
    def post(self, request):
        if not request.user.is_superuser:
            return Response({"error": "Only admins can create new users."}, status=status.HTTP_403_FORBIDDEN)

        data = request.data
        email = data.get("email", "").strip().lower()
        organisation = data.get("organisation")
        contact_name = data.get("contact_name")
        contact_phone = data.get("contact_phone")
        contact_email = data.get("contact_email", email)
        address = data.get("address", "")
        industry = data.get("industry", "")
        use_case = data.get("use_case", "")
        role_name = data.get("role", "Guest")  # Default to "Guest"

        # ✅ Generate a secure temporary password
        temp_password = uuid.uuid4().hex[:12]  # Generates a 12-character password

        # ✅ Fix: Remove password from required fields
        if not email or not organisation or not contact_name or not contact_phone:
            return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Ensure email is unique
        if User.objects.filter(email=email).exists():
            return Response({"error": "Email is already registered"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Get or create the client object
        client, _ = Client.objects.get_or_create(name=organisation)

        try:
            # ✅ Create user with a temporary password
            user = User.objects.create_user(
                email=email,
                password=temp_password,  # ✅ Assign the temporary password
                client=client,
            )
            user.contact_name = contact_name
            user.contact_phone = contact_phone
            user.contact_email = contact_email
            user.address = address
            user.industry = industry
            user.use_case = use_case
            user.profile_created_at = now()
            user.save()

            # ✅ Assign role (Admin, Manager, Guest)
            role, _ = Group.objects.get_or_create(name=role_name)
            user.groups.add(role)

            # ✅ Generate OAuth2 credentials
            raw_client_secret = generate_client_secret()
            app = Application.objects.create(
                user=user,
                name=f"{user.client.name} - API Access",
                client_type=Application.CLIENT_CONFIDENTIAL,
                # authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
                authorization_grant_type=Application.GRANT_PASSWORD,
                client_secret=raw_client_secret,
            )
            app.save()

            # ✅ Store raw secret securely
            store_client_secret(app.client_id, raw_client_secret)

            # ✅ Trigger Celery email task
            send_signup_email_task.delay(
                user.id,
                temp_password,
                app.client_id,
                raw_client_secret
            )

            # ✅ Trigger Nextcloud auto-provisioning
            provision_nextcloud_user.delay(user.id)

            return Response({
                "message": "User created successfully",
                "user_id": user.id,
                "email": user.email,
                "temporary_password": temp_password,  # ✅ Include the generated password
                "organisation": user.client.name if user.client else None,
                "role": role.name,
                "client_id": app.client_id,
                "client_secret": raw_client_secret,
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"❌ Error during user creation: {str(e)}")
            return Response({"error": "Internal Server Error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RetrieveUserView(APIView):
    authentication_classes = [OAuth2Authentication]
    # permission_classes = [IsAdminUser]  # ✅ Only admins can access
    permission_classes = [IsAdminOrSuperUser]

    def get(self, request, user_id):
        try:
            user = _get_managed_user(request, user_id)
            return Response({
                "id": user.id,
                "email": user.email,
                "organisation": user.client.name if user.client else None,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,  # ✅ Check if they are an admin
                "created_at": user.profile_created_at,
            }, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class RetrieveClientSecretsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    def get(self, request, user_id):
        """
        Retrieve OAuth2 client application credentials for a specific user (Admin-Only).
        Includes stored client_secret (from secrets file) if available.
        """
        applications = Application.objects.filter(user_id=user_id)

        if not applications.exists():
            return Response(
                {"error": "No OAuth credentials found for this user"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Attempt to load stored client secrets from JSON
        try:
            with open(SECRETS_FILE_PATH, "r") as file:
                stored_secrets = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            stored_secrets = {}

        # Serialize applications using your ClientApplicationSerializer
        serializer = ClientApplicationSerializer(applications, many=True)
        response_data = serializer.data

        # Inject the unhashed client_secret from the stored secrets
        for app in response_data:
            client_id = app["client_id"]
            app["client_secret"] = stored_secrets.get(client_id, "*********")  # Obfuscate if not found

        return Response(response_data, status=status.HTTP_200_OK)


class RetrieveClientView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    def get(self, request, client_id):
        try:
            client = Client.objects.get(id=client_id)
            return Response({
                "id": client.id,
                "name": client.name,
                "address": client.address,
                "industry": client.industry,
                "use_case": client.use_case,
                "created_at": client.created_at,
                "user_count": client.users.count(),  # Optional: number of associated users
            }, status=status.HTTP_200_OK)
        except Client.DoesNotExist:
            return Response({"error": "Client not found"}, status=status.HTTP_404_NOT_FOUND)



class TwoFASetupView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        # Generate secret
        if not user.totp_secret:
            secret = pyotp.random_base32()
            user.totp_secret = secret
            user.save()
        else:
            secret = user.totp_secret

        # Generate provisioning URI
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="AI DocuMines")

        # Generate QR code
        qr = qrcode.make(uri)
        buffer = io.BytesIO()
        qr.save(buffer, format='PNG')
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()

        return Response({
            "secret": secret,
            "otpauth_url": uri,
            "qr_code_base64": qr_base64
        })


class TwoFAVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        code = request.data.get("code")
        user = request.user

        if not user.totp_secret:
            return Response({"error": "TOTP is not set up for this user."}, status=400)

        totp = pyotp.TOTP(user.totp_secret)

        if totp.verify(code):
            user.is_2fa_enabled = True
            user.is_2fa_verified = True
            user.save()
            return Response({"message": "2FA successfully verified and enabled."})
        else:
            return Response({"error": "Invalid verification code."}, status=400)




class TwoFAStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"2fa_enabled": request.user.is_2fa_enabled})



class TwoFADisableView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        user.is_2fa_enabled = False
        user.is_2fa_verified = False
        user.totp_secret = None
        user.save()
        return Response({"message": "2FA has been disabled."})


class TwoFARotateSecretView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        # Generate a new secret
        new_secret = pyotp.random_base32()
        user.totp_secret = new_secret
        user.is_2fa_enabled = False
        user.is_2fa_verified = False
        user.save()

        # Provision URI
        uri = pyotp.totp.TOTP(new_secret).provisioning_uri(
            name=user.email, issuer_name="AI DocuMines"
        )

        # QR Code
        qr = qrcode.make(uri)
        buffer = io.BytesIO()
        qr.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()

        return Response({
            "message": "2FA secret rotated. Please scan the new QR code.",
            "otp_secret": new_secret,
            "otp_uri": uri,
            "qr_code_base64": qr_base64
        })




class WhoAmIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Verified identity for the token holder. Used by the frontend and by the
        Node backend to derive user, tenant and RBAC server-side.
        """
        from core.access import user_role_names, primary_role, user_permissions, is_tenant_admin, is_platform_superuser
        from oauth2_provider.models import Application

        user = request.user
        return Response({
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "roles": sorted(user_role_names(user)),
            "primary_role": primary_role(user),
            "permissions": sorted(user_permissions(user)),
            "client": user.client.name if user.client else None,
            "client_org_id": user.client_id,
            "oauth_client_ids": list(Application.objects.filter(user=user).values_list("client_id", flat=True)),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "is_tenant_admin": is_tenant_admin(user),
            "is_platform_superuser": is_platform_superuser(user),
            "2fa_enabled": user.is_2fa_enabled,
        })


class LogoutAllView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        AccessToken.objects.filter(user=request.user).delete()
        logout(request)
        return Response({"message": "Logged out from all sessions."}, status=200)


class DemoteUserFromAdminView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    def post(self, request, user_id):
        try:
            user = _get_managed_user(request, user_id)

            if not user.is_superuser:
                return Response({"message": "User is not an admin"}, status=status.HTTP_400_BAD_REQUEST)

            # ✅ Revoke elevated permissions
            user.is_staff = False
            user.is_superuser = False
            user.save()

            # ✅ Adjust group roles
            admin_group, _ = Group.objects.get_or_create(name="Admin")
            guest_group, _ = Group.objects.get_or_create(name="Guest")

            user.groups.remove(admin_group)
            user.groups.add(guest_group)

            return Response({"message": f"User {user.email} demoted to Guest"}, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class SupersuperuserResetView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        import os
        # Break-glass endpoints are disabled unless explicitly enabled on the server.
        if os.environ.get("ENABLE_BREAK_GLASS_ENDPOINTS", "false").lower() != "true":
            from django.http import Http404
            raise Http404()
        import json
        from django.contrib.auth import get_user_model
        from custom_authentication.models import Client
        from oauth2_provider.models import Application
        from oauth2_provider.generators import generate_client_secret
        from django.conf import settings
        from rest_framework.response import Response
        from rest_framework import status
        import logging

        logger = logging.getLogger(__name__)
        User = get_user_model()

        # Step 1: Get credentials from request
        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return Response({"error": "Email and password are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"error": "Invalid email or password"}, status=status.HTTP_403_FORBIDDEN)

        if not user.check_password(password):
            return Response({"error": "Invalid email or password"}, status=status.HTTP_403_FORBIDDEN)

        # Step 2: Verify against .env superuser (no hardcoded fallbacks)
        ADMIN_EMAIL = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        ADMIN_PASSWORD = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        SECRETS_FILE_PATH = "logs/.superuser_secrets.json"

        if not ADMIN_EMAIL or not ADMIN_PASSWORD:
            return Response({"error": "Break-glass reset is not configured."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not user.is_superuser or user.email.lower() != ADMIN_EMAIL.lower():
            return Response({"error": "Unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        try:
            # Step 3: Delete all other superusers
            User.objects.filter(is_superuser=True).exclude(id=user.id).delete()

            # Step 4: Create or get the Client
            client, _ = Client.objects.get_or_create(
                name="AI DocuMines",
                defaults={
                    "address": "Admin Address",
                    "industry": "AI Research",
                    "use_case": "System setup"
                }
            )

            # Step 5: Reuse or recreate the superuser from .env
            if user.email.lower() == ADMIN_EMAIL.lower():
                new_superuser = user
            else:
                User.objects.filter(email=ADMIN_EMAIL).exclude(id=user.id).delete()
                new_superuser = User.objects.create_superuser(
                    email=ADMIN_EMAIL,
                    password=ADMIN_PASSWORD,
                    client=client,
                    contact_name="Admin User",
                    contact_phone="+27 000 000 000",
                    address="Admin Address",
                    industry="AI Research",
                    use_case="System setup"
                )

            # Step 6: Create new OAuth2 app
            raw_client_secret = generate_client_secret()
            app = Application.objects.create(
                user=new_superuser,
                client_type=Application.CLIENT_CONFIDENTIAL,
                authorization_grant_type=Application.GRANT_PASSWORD,
                name="System Admin API Access",
                client_secret=raw_client_secret
            )

            # Step 7: Store to secrets file
            os.makedirs("logs", exist_ok=True)
            with open(SECRETS_FILE_PATH, "w") as f:
                json.dump({
                    "admin_email": ADMIN_EMAIL,
                    "admin_password": ADMIN_PASSWORD,
                    "client_id": app.client_id,
                    "client_secret": raw_client_secret
                }, f, indent=4)
            os.chmod(SECRETS_FILE_PATH, 0o600)

            logger.info(f"✅ Supersuperuser system reset performed by {user.email}")

            return Response({
                "message": "Supersuperuser-triggered system reset complete",
                "admin_email": ADMIN_EMAIL,
                "client_id": app.client_id,
                "client_secret": raw_client_secret
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"❌ Supersuperuser reset failed: {e}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SupersuperuserCleanDatabaseView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        import os
        # Break-glass endpoints are disabled unless explicitly enabled on the server.
        if os.environ.get("ENABLE_BREAK_GLASS_ENDPOINTS", "false").lower() != "true":
            from django.http import Http404
            raise Http404()
        from django.db import connection
        from django.contrib.auth import get_user_model
        from rest_framework.response import Response
        from rest_framework import status
        import logging

        logger = logging.getLogger(__name__)

        email = request.data.get("email")
        password = request.data.get("password")

        if not email or not password:
            return Response({"error": "Email and password are required"}, status=400)

        User = get_user_model()

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"error": "Invalid credentials"}, status=403)

        if not user.check_password(password):
            return Response({"error": "Invalid credentials"}, status=403)

        # 🧠 Must match env-based supersuperuser
        env_email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
        if not env_email or not user.is_superuser or user.email.lower() != env_email.lower():
            return Response({"error": "Unauthorized"}, status=403)

        try:
            with connection.cursor() as cursor:
                logger.info("🔄 Starting full DB cleanup...")

                # Get all public tables, excluding migrations
                cursor.execute("""
                    SELECT tablename FROM pg_tables
                    WHERE schemaname = 'public'
                      AND tablename != 'django_migrations';
                """)
                all_tables = [row[0] for row in cursor.fetchall()]

                # ⚠️ Tables to preserve (DO NOT DELETE/RESET)
                preserved_tables = [
                    "custom_authentication_customuser",  # keep supersuperuser
                    "oauth2_provider_application"       # optionally keep app metadata
                ]

                # Safely DELETE all other users first (manually)
                cursor.execute("DELETE FROM custom_authentication_customuser WHERE email != %s;", [env_email])

                # Disable foreign key constraints
                cursor.execute("SET session_replication_role = replica;")

                # Truncate only safe tables
                tables_to_truncate = [
                    t for t in all_tables if t not in preserved_tables
                ]

                for table in tables_to_truncate:
                    try:
                        cursor.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE;')
                    except Exception as e:
                        logger.warning(f"⚠️ Could not truncate table {table}: {e}")

                # Re-enable FK constraints
                cursor.execute("SET session_replication_role = origin;")

                logger.info("✅ Database cleanup complete.")
                return Response({
                    "message": f"✅ Database cleaned. Only superuser '{env_email}' retained.",
                    "tables_cleaned": len(tables_to_truncate)
                }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"❌ Cleanup failed: {e}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ═══════════════════════════════════════════════════════════════════════════════
# ROLE MANAGEMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# Define the available roles in the system
AVAILABLE_ROLES = [
    {"name": "Admin", "description": "Full administrative access", "level": 100},
    {"name": "Manager", "description": "Can manage users and projects within their client", "level": 80},
    {"name": "Developer", "description": "Technical access for API integrations", "level": 60},
    {"name": "Client", "description": "Standard client user access", "level": 40},
    {"name": "Guest", "description": "Limited read-only access", "level": 20},
]

ROLE_NAMES = [role["name"] for role in AVAILABLE_ROLES]


class ListRolesView(APIView):
    """List all available roles in the system."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="List all available roles that can be assigned to users.",
        tags=["Admin - Roles"],
        responses={200: "List of available roles"},
    )
    def get(self, request):
        # Ensure all roles exist in the database
        for role in AVAILABLE_ROLES:
            Group.objects.get_or_create(name=role["name"])

        return Response({
            "roles": AVAILABLE_ROLES,
            "total": len(AVAILABLE_ROLES)
        }, status=status.HTTP_200_OK)


class GetUserRolesView(APIView):
    """Get all roles assigned to a specific user."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Get all roles assigned to a specific user.",
        tags=["Admin - Roles"],
        responses={200: "User roles", 404: "User not found"},
    )
    def get(self, request, user_id):
        try:
            user = _get_managed_user(request, user_id)
            user_roles = list(user.groups.values_list("name", flat=True))

            return Response({
                "user_id": user.id,
                "email": user.email,
                "roles": user_roles,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            }, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class AssignUserRoleView(APIView):
    """Assign a role to a user."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Assign a role to a user. Multiple roles can be assigned.",
        tags=["Admin - Roles"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "role": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Role name to assign",
                    enum=ROLE_NAMES
                ),
                "set_staff": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Also set is_staff=True (for Admin role)",
                    default=False
                ),
                "set_superuser": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Also set is_superuser=True (for Admin role)",
                    default=False
                ),
            },
            required=["role"],
        ),
        responses={200: "Role assigned", 400: "Invalid role", 404: "User not found"},
    )
    def post(self, request, user_id):
        role_name = request.data.get("role")
        set_staff = request.data.get("set_staff", False)
        set_superuser = request.data.get("set_superuser", False)

        if not role_name:
            return Response({"error": "Role name is required"}, status=status.HTTP_400_BAD_REQUEST)

        if role_name not in ROLE_NAMES:
            return Response({
                "error": f"Invalid role '{role_name}'. Available roles: {ROLE_NAMES}"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = _get_managed_user(request, user_id)

            # Platform role: only platform superusers may grant it
            if role_name == "SuperAdmin" and not _can_grant_platform_roles(request):
                return Response({"error": "Only platform superusers can assign the SuperAdmin role"},
                                status=status.HTTP_403_FORBIDDEN)

            # Prevent non-superusers from assigning Admin role
            if role_name == "Admin" and not request.user.is_superuser:
                return Response({
                    "error": "Only superusers can assign the Admin role"
                }, status=status.HTTP_403_FORBIDDEN)

            # Get or create the role group
            role_group, _ = Group.objects.get_or_create(name=role_name)

            # Add the role to the user
            user.groups.add(role_group)

            # Optionally set staff/superuser flags
            if set_staff or role_name == "Admin":
                user.is_staff = True
            if set_superuser:
                if not request.user.is_superuser:
                    return Response({
                        "error": "Only superusers can grant superuser status"
                    }, status=status.HTTP_403_FORBIDDEN)
                user.is_superuser = True

            user.save()

            # Log the activity
            log_activity(user, "ROLE_ASSIGNED", {
                "role": role_name,
                "assigned_by": request.user.id,
                "assigned_by_email": request.user.email
            })

            return Response({
                "message": f"Role '{role_name}' assigned to user {user.email}",
                "user_id": user.id,
                "email": user.email,
                "roles": list(user.groups.values_list("name", flat=True)),
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            }, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class RemoveUserRoleView(APIView):
    """Remove a role from a user."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Remove a role from a user.",
        tags=["Admin - Roles"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "role": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Role name to remove",
                    enum=ROLE_NAMES
                ),
                "revoke_staff": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Also set is_staff=False",
                    default=False
                ),
                "revoke_superuser": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Also set is_superuser=False",
                    default=False
                ),
            },
            required=["role"],
        ),
        responses={200: "Role removed", 400: "Invalid role", 404: "User not found"},
    )
    def post(self, request, user_id):
        role_name = request.data.get("role")
        revoke_staff = request.data.get("revoke_staff", False)
        revoke_superuser = request.data.get("revoke_superuser", False)

        if not role_name:
            return Response({"error": "Role name is required"}, status=status.HTTP_400_BAD_REQUEST)

        if role_name not in ROLE_NAMES:
            return Response({
                "error": f"Invalid role '{role_name}'. Available roles: {ROLE_NAMES}"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = _get_managed_user(request, user_id)

            # Prevent removing Admin role from self
            if role_name == "Admin" and user.id == request.user.id:
                return Response({
                    "error": "You cannot remove the Admin role from yourself"
                }, status=status.HTTP_400_BAD_REQUEST)

            # Prevent non-superusers from removing Admin role
            if role_name == "Admin" and not request.user.is_superuser:
                return Response({
                    "error": "Only superusers can remove the Admin role"
                }, status=status.HTTP_403_FORBIDDEN)

            # Get the role group
            try:
                role_group = Group.objects.get(name=role_name)
                user.groups.remove(role_group)
            except Group.DoesNotExist:
                pass  # Role doesn't exist, nothing to remove

            # Optionally revoke staff/superuser flags
            if revoke_staff:
                user.is_staff = False
            if revoke_superuser:
                if not request.user.is_superuser:
                    return Response({
                        "error": "Only superusers can revoke superuser status"
                    }, status=status.HTTP_403_FORBIDDEN)
                user.is_superuser = False

            # If removing Admin role, also revoke elevated permissions
            if role_name == "Admin":
                user.is_staff = False
                user.is_superuser = False
                # Assign Guest role as fallback
                guest_group, _ = Group.objects.get_or_create(name="Guest")
                user.groups.add(guest_group)

            user.save()

            # Log the activity
            log_activity(user, "ROLE_REMOVED", {
                "role": role_name,
                "removed_by": request.user.id,
                "removed_by_email": request.user.email
            })

            return Response({
                "message": f"Role '{role_name}' removed from user {user.email}",
                "user_id": user.id,
                "email": user.email,
                "roles": list(user.groups.values_list("name", flat=True)),
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            }, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)


class SetUserRolesView(APIView):
    """Set all roles for a user (replaces existing roles)."""
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Set all roles for a user. This replaces all existing roles.",
        tags=["Admin - Roles"],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "roles": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_STRING, enum=ROLE_NAMES),
                    description="List of role names to set"
                ),
                "set_staff": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Set is_staff flag",
                    default=False
                ),
                "set_superuser": openapi.Schema(
                    type=openapi.TYPE_BOOLEAN,
                    description="Set is_superuser flag",
                    default=False
                ),
            },
            required=["roles"],
        ),
        responses={200: "Roles set", 400: "Invalid roles", 404: "User not found"},
    )
    def post(self, request, user_id):
        role_names = request.data.get("roles", [])
        set_staff = request.data.get("set_staff", False)
        set_superuser = request.data.get("set_superuser", False)

        if not role_names:
            return Response({"error": "At least one role is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate all roles
        invalid_roles = [r for r in role_names if r not in ROLE_NAMES]
        if invalid_roles:
            return Response({
                "error": f"Invalid roles: {invalid_roles}. Available roles: {ROLE_NAMES}"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = _get_managed_user(request, user_id)

            if "SuperAdmin" in role_names and not _can_grant_platform_roles(request):
                return Response({"error": "Only platform superusers can assign the SuperAdmin role"},
                                status=status.HTTP_403_FORBIDDEN)

            # Check Admin role assignment permissions
            if "Admin" in role_names and not request.user.is_superuser:
                return Response({
                    "error": "Only superusers can assign the Admin role"
                }, status=status.HTTP_403_FORBIDDEN)

            # Prevent removing Admin role from self
            current_roles = set(user.groups.values_list("name", flat=True))
            if "Admin" in current_roles and "Admin" not in role_names and user.id == request.user.id:
                return Response({
                    "error": "You cannot remove the Admin role from yourself"
                }, status=status.HTTP_400_BAD_REQUEST)

            # Clear all existing roles
            user.groups.clear()

            # Add new roles
            for role_name in role_names:
                role_group, _ = Group.objects.get_or_create(name=role_name)
                user.groups.add(role_group)

            # Set staff/superuser flags based on roles
            if "Admin" in role_names:
                user.is_staff = True
                if set_superuser:
                    if not request.user.is_superuser:
                        return Response({
                            "error": "Only superusers can grant superuser status"
                        }, status=status.HTTP_403_FORBIDDEN)
                    user.is_superuser = True
            else:
                user.is_staff = set_staff
                if set_superuser:
                    if not request.user.is_superuser:
                        return Response({
                            "error": "Only superusers can grant superuser status"
                        }, status=status.HTTP_403_FORBIDDEN)
                    user.is_superuser = set_superuser
                else:
                    user.is_superuser = False

            user.save()

            # Log the activity
            log_activity(user, "ROLES_SET", {
                "roles": role_names,
                "set_by": request.user.id,
                "set_by_email": request.user.email
            })

            return Response({
                "message": f"Roles set for user {user.email}",
                "user_id": user.id,
                "email": user.email,
                "roles": list(user.groups.values_list("name", flat=True)),
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            }, status=status.HTTP_200_OK)

        except CustomUser.DoesNotExist:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
