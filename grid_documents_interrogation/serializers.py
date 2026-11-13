from rest_framework import serializers
from .models import Topic, Query, DatabaseConnection
from core.models import File
from django.contrib.auth import get_user_model
from document_operations.utils import has_file_access

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
        queryset=DatabaseConnection.objects.none(),
        write_only=True,
        source='db_connection',
        required=False,
        allow_null=True
    )
    files = FileSerializer(many=True, read_only=True)
    has_data_source = serializers.BooleanField(read_only=True)
    latest_query = serializers.SerializerMethodField()

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
            'has_data_source',
            'latest_query',
        ]
        read_only_fields = ['id', 'user', 'chat_date', 'db_connection', 'files', 'has_data_source', 'latest_query']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        field = self.fields.get("db_connection_id")
        if not field or not user or not user.is_authenticated:
            return
        qs = DatabaseConnection.objects.filter(owner=user)
        client_id = getattr(user, "client_id", None)
        if client_id:
            qs = qs.filter(owner__client_id=client_id)
        field.queryset = qs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        conn = instance.db_connection
        if conn and (not user or conn.owner_id != user.id):
            data["db_connection"] = None
        if user and data.get("files"):
            allowed = {
                f.id for f in instance.files.all()
                if has_file_access(user, f, "read")
            }
            data["files"] = [item for item in data["files"] if item.get("id") in allowed]
        return data

    def get_latest_query(self, obj):
        text = getattr(obj, "latest_query_text", None) or ""
        return text[:140]

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

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        topic = attrs.get("topic") or getattr(self.instance, "topic", None)
        if user and topic and topic.user_id != user.id:
            raise serializers.ValidationError({"topic": "Not found."})
        file = attrs.get("file", None)
        if user and file is not None and not has_file_access(user, file, "read"):
            raise serializers.ValidationError({"file": "Not found."})
        return attrs

