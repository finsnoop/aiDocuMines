from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    PasswordResetToken,
    RefreshToken,
    AccountDeactivationRequest,
    UserActivityLog
)
from oauth2_provider.models import Application

User = get_user_model()


class SignupSerializer(serializers.ModelSerializer):
    """Serializer for user registration."""

    password = serializers.CharField(write_only=True)
    organisation = serializers.CharField(write_only=True)  # for creating Client
    contact_name = serializers.CharField()
    contact_phone = serializers.CharField()
    contact_email = serializers.EmailField()
    address = serializers.CharField(required=False, allow_blank=True)
    industry = serializers.CharField(required=False, allow_blank=True)
    use_case = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "email", "password", "organisation",
            "contact_name", "contact_phone", "contact_email",
            "address", "industry", "use_case"
        ]

    def create(self, validated_data):
        password = validated_data.pop("password")
        organisation_name = validated_data.pop("organisation")

        client, _ = Client.objects.get_or_create(name=organisation_name)

        user = User.objects.create_user(client=client, **validated_data)
        user.set_password(password)
        user.save()
        return user
    

class ClientApplicationSerializer(serializers.ModelSerializer):
    client_name = serializers.CharField(source='name')
    created_at = serializers.DateTimeField(source='created', read_only=True)
    updated_at = serializers.DateTimeField(source='updated', read_only=True)

    class Meta:
        model = Application
        fields = [
            "id", "client_id", "client_name", "client_type",
            "authorization_grant_type", "created_at", "updated_at"
        ]


class LoginSerializer(serializers.Serializer):
    """Handles user authentication."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        """Authenticate user with email & password."""
        from django.contrib.auth import authenticate  # Lazy import to prevent circular import

        user = authenticate(email=data["email"], password=data["password"])
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    """Serializer for requesting password reset."""

    email = serializers.EmailField()

    def validate_email(self, value):
        """Ensure user exists before sending reset link."""
        if not User.objects.filter(email=value).exists():
            raise serializers.ValidationError("User not found")
        return value


class PasswordResetSerializer(serializers.Serializer):
    """Serializer for resetting password using a token."""

    token = serializers.UUIDField()
    new_password = serializers.CharField(write_only=True)

    def validate(self, data):
        """Check if the token is valid and reset password."""
        try:
            token_obj = PasswordResetToken.objects.get(token=data["token"])
        except PasswordResetToken.DoesNotExist:
            raise serializers.ValidationError("Invalid reset token")

        if not token_obj.is_valid():
            raise serializers.ValidationError("Reset token expired or already used")

        return {"user": token_obj.user, "new_password": data["new_password"]}


class RefreshTokenSerializer(serializers.Serializer):
    """Serializer for refreshing an expired access token."""

    refresh_token = serializers.CharField()

    def validate_refresh_token(self, value):
        """Check if refresh token is valid."""
        try:
            token_obj = RefreshToken.objects.get(token=value)
        except RefreshToken.DoesNotExist:
            raise serializers.ValidationError("Invalid refresh token")

        if not token_obj.is_valid():
            raise serializers.ValidationError("Refresh token has expired")

        return token_obj


class UserProfileSerializer(serializers.ModelSerializer):
    """Serializer for retrieving and updating user profile."""

    client_name = serializers.CharField(source='client.name', read_only=True)
    
    class Meta:
        model = User
        fields = [
            "email", "client_name", "contact_name",
            "contact_phone", "address", "industry", "use_case"
        ]


class ChangePasswordSerializer(serializers.Serializer):
    """Serializer for changing a user's password."""

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        """Ensure the current password is correct."""
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Incorrect current password")
        return value


class DeactivateAccountSerializer(serializers.ModelSerializer):
    """Serializer for deactivating a user account."""

    class Meta:
        model = AccountDeactivationRequest
        fields = ["requested_at", "is_processed"]


class AdminUserSerializer(serializers.ModelSerializer):
    """Serializer for listing users in the admin panel."""

    client_name = serializers.CharField(source='client.name', read_only=True)
    
    class Meta:
        model = User
        fields = ["id", "email", "client_name", "is_active"]
    

class AdminResetUserPasswordSerializer(serializers.Serializer):
    """Serializer for admin resetting a user's password."""

    user_id = serializers.IntegerField()
    new_password = serializers.CharField(write_only=True)


class CreateAdminUserSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=6)
    organisation = serializers.CharField()
    contact_name = serializers.CharField()
    contact_phone = serializers.CharField()
    address = serializers.CharField()
    industry = serializers.CharField()
    use_case = serializers.CharField()


class UserActivityLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserActivityLog
        fields = ['user', 'event', 'timestamp', 'metadata']
