# cost_centre/serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model

from custom_authentication.models import Client  # your tenant model
from .models import EventLog, TokenUsage, Budget, Subscription, PaymentHistory

User = get_user_model()


# ---------- Common / Nested ----------
class UserSerializer(serializers.ModelSerializer):
    # Works with your CustomUser (USERNAME_FIELD = "email")
    username = serializers.CharField(source="get_username", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]


class TenantSerializer(serializers.ModelSerializer):
    # Keep API name 'Tenant', but model is Client
    class Meta:
        model = Client
        fields = ["id", "name", "address", "industry", "use_case", "created_at"]


# ---------- EventLog ----------
class EventLogSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    tenant = TenantSerializer(read_only=True)

    # Write-friendly *_id fields
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user", write_only=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), source="tenant", write_only=True
    )

    class Meta:
        model = EventLog
        fields = [
            "id",
            "user", "tenant",
            "user_id", "tenant_id",
            "event_type", "metadata", "tokens_used", "service_type",
            "idempotency_key",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        tokens_used = data.get("tokens_used", getattr(self.instance, "tokens_used", 0)) or 0
        service_type = data.get("service_type", getattr(self.instance, "service_type", None))
        if tokens_used > 0 and service_type == "non_payable":
            raise serializers.ValidationError(
                "Token usage cannot be associated with a non-payable service."
            )
        return data


# ---------- TokenUsage ----------
class TokenUsageSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    tenant = TenantSerializer(read_only=True)
    event_log = EventLogSerializer(read_only=True, required=False)

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user", write_only=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), source="tenant", write_only=True
    )
    event_log_id = serializers.PrimaryKeyRelatedField(
        queryset=EventLog.objects.all(),
        source="event_log",
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = TokenUsage
        fields = [
            "id",
            "user", "tenant", "event_log",
            "user_id", "tenant_id", "event_log_id",
            "tokens_used",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ---------- Budget ----------
class BudgetSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    tenant = TenantSerializer(read_only=True)

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user", write_only=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), source="tenant", write_only=True
    )

    class Meta:
        model = Budget
        fields = [
            "id",
            "user", "tenant",
            "user_id", "tenant_id",
            "token_limit", "financial_limit",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        tok = data.get("token_limit")
        fin = data.get("financial_limit")
        if tok is not None and tok < 0:
            raise serializers.ValidationError("Token limit cannot be negative.")
        if fin is not None and fin < 0:
            raise serializers.ValidationError("Financial limit cannot be negative.")
        return data


# ---------- Subscription ----------
class SubscriptionSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    tenant = TenantSerializer(read_only=True)
    is_active = serializers.BooleanField(read_only=True)  # property on model

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user", write_only=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), source="tenant", write_only=True
    )

    class Meta:
        model = Subscription
        fields = [
            "id",
            "user", "tenant",
            "user_id", "tenant_id",
            "stripe_subscription_id", "stripe_payment_method_id", "stripe_status",
            "plan_code", "seat_count", "annual_prepay",
            "amount_billed", "billing_cycle_start", "billing_cycle_end",
            "tokens_item_id", "pages_item_id", "translation_item_id", "redact_item_id",
            "is_active",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        allowed = {"active", "inactive", "trialing", "canceled", "past_due"}
        status = data.get("stripe_status", getattr(self.instance, "stripe_status", None))
        if status and status not in allowed:
            raise serializers.ValidationError("Invalid subscription status.")
        return data


# ---------- PaymentHistory ----------
class PaymentHistorySerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    tenant = TenantSerializer(read_only=True)

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user", write_only=True
    )
    tenant_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), source="tenant", write_only=True
    )

    class Meta:
        model = PaymentHistory
        fields = [
            "id",
            "user", "tenant",
            "user_id", "tenant_id",
            "amount_paid", "currency", "payment_date", "payment_method", "stripe_payment_intent",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, data):
        amt = data.get("amount_paid")
        if amt is not None and amt < 0:
            raise serializers.ValidationError("Payment amount cannot be negative.")
        return data

