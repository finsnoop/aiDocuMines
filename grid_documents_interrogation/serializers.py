from rest_framework import serializers
from .models import Topic, Query, DatabaseConnection
from core.models import File
from django.contrib.auth import get_user_model

User = get_user_model()


class DatabaseConnectionSerializer(serializers.ModelSerializer):
    owner = serializers.StringRelatedField(read_only=True)
    password = serializers.CharField(write_only=True)

    class Meta:
        model = DatabaseConnection
        fields = [
            'id',
            'name',
            'database_type',
            'host',
            'port',
            'username',
            'password',          # <- required for POST
            'database_name',
            'created_at',
            'owner'
        ]
        read_only_fields = ['id', 'created_at', 'owner']

    def validate_database_type(self, value):
        if value not in ("postgres", "mysql"):
            raise serializers.ValidationError("Only PostgreSQL and MySQL databases can be connected.")
        return value

    def validate_port(self, value):
        if not 1 <= int(value) <= 65535:
            raise serializers.ValidationError("Enter a port between 1 and 65535.")
        return value

    def validate_host(self, value):
        from .safety import UnsafeTarget, check_host
        try:
            check_host(value)
        except UnsafeTarget as exc:
            raise serializers.ValidationError(str(exc))
        return value.strip()

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        instance = DatabaseConnection(**validated_data)
        if password is not None:
            instance.password = password  # 🔒 force trigger encryption
        instance.save()
        return instance


class FileSerializer(serializers.ModelSerializer):
    class Meta:
        model = File
        fields = ['id', 'filename', 'created_at']


class TopicSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    db_connection = DatabaseConnectionSerializer(read_only=True)
    db_connection_id = serializers.PrimaryKeyRelatedField(
        queryset=DatabaseConnection.objects.all(),
        write_only=True,
        source='db_connection',
        required=False,
        allow_null=True
    )
    files = FileSerializer(many=True, read_only=True)
    has_data_source = serializers.BooleanField(read_only=True)  # ✅ FIXED: removed `source=...`

    def get_fields(self):
        fields = super().get_fields()
        # A topic may only be linked to the caller's own saved connections.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        fields["db_connection_id"].queryset = (
            DatabaseConnection.objects.filter(owner=user) if user and user.is_authenticated
            else DatabaseConnection.objects.none())
        return fields

    class Meta:
        model = Topic
        fields = [
            'id',
            'name',
            'project_id',
            'service_id',
            'user',
            'chat_date',
            'db_connection',
            'db_connection_id',
            'files',
            'has_data_source'
        ]
        read_only_fields = ['id', 'user', 'chat_date', 'db_connection', 'files', 'has_data_source']

'''
class TopicSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    db_connection = DatabaseConnectionSerializer(read_only=True)
    db_connection_id = serializers.PrimaryKeyRelatedField(
        queryset=DatabaseConnection.objects.all(),
        write_only=True,
        source='db_connection',
        required=False,
        allow_null=True
    )
    files = FileSerializer(many=True, read_only=True)
    has_data_source = serializers.BooleanField(source='has_data_source', read_only=True)

    class Meta:
        model = Topic
        fields = [
            'id',
            'name',
            'project_id',
            'service_id',
            'user',
            'chat_date',
            'db_connection',
            'db_connection_id',
            'files',
            'has_data_source'
        ]
        read_only_fields = ['id', 'user', 'chat_date', 'db_connection', 'files', 'has_data_source']
'''


class QuerySerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    topic = serializers.PrimaryKeyRelatedField(queryset=Topic.objects.all())
    file = serializers.PrimaryKeyRelatedField(queryset=File.objects.all(), required=False, allow_null=True)

    class Meta:
        model = Query
        fields = [
            'id',
            'topic',
            'user',
            'file',
            'query_text',
            'response_text',
            'created_at',
        ]
        read_only_fields = ['id', 'user', 'created_at']

