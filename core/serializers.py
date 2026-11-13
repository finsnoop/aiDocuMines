from rest_framework import serializers
from .models import Run, File, Storage, Metadata, Webhook
from core.utils import get_logical_path
# from django.contrib.auth.models import User

from django.contrib.auth import get_user_model

User = get_user_model()



class RunSerializer(serializers.ModelSerializer):
    """Serializer for Run model"""

    class Meta:
        model = Run
        fields = [
            "run_id",  # ✅ Fixed reference from "id" to "run_id"
            "user",
            "status",
            "unique_code",
            "characters",
            "cost",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["run_id", "unique_code", "created_at", "updated_at"]

    def validate_status(self, value):
        """Ensure status is a valid choice"""
        valid_statuses = dict(Run._meta.get_field("status").choices).keys()
        if value not in valid_statuses:
            raise serializers.ValidationError(f"Invalid status: {value}")
        return value


class FileSerializer(serializers.ModelSerializer):
    """Serializer for File model"""

    run_id = serializers.UUIDField(source="run.run_id", read_only=True)  # ✅ Referencing `run_id`
    storage = serializers.SerializerMethodField()  # ✅ Include related storage information

    # Add these fields with null-safe defaults
    extension = serializers.SerializerMethodField()
    document_type = serializers.SerializerMethodField()
    project_id = serializers.SerializerMethodField()
    service_id = serializers.SerializerMethodField()
    filepath = serializers.SerializerMethodField()

    class Meta:
        model = File
        fields = [
            "id",
            "filename",
            "filepath",
            "total_characters",
            "cost",
            "unique_code",
            "status",
            "run_id",
            "storage",
            "created_at",
            "updated_at",
            "extension",
            "document_type",
            "project_id",
            "service_id"
            ]
        read_only_fields = ["id", "unique_code", "created_at", "updated_at"]

    def get_filepath(self, obj):
        """Never expose the raw server filesystem path - return a logical path."""
        return get_logical_path(obj.filepath, obj.filename)

    def get_storage(self, obj):
        """Retrieve related storage details"""
        if hasattr(obj, "storage"):
            return StorageSerializer(obj.storage).data
        return None

    def get_extension(self, obj):
        return obj.extension or "unknown"

    def get_document_type(self, obj):
        return obj.document_type or "Unknown"

    def get_project_id(self, obj):
        return obj.project_id or "unknown_project"

    def get_service_id(self, obj):
        return obj.service_id or "unknown_service"

    def validate_status(self, value):
        """Ensure file status is a valid choice"""
        valid_statuses = dict(File._meta.get_field("status").choices).keys()
        if value not in valid_statuses:
            raise serializers.ValidationError(f"Invalid file status: {value}")
        return value


class FileUploadSerializer(serializers.Serializer):
    """Serializer for file uploads"""

    run_id = serializers.UUIDField()  # ✅ Ensuring run_id is treated as a UUID
    files = serializers.ListField(
        child=serializers.FileField(),
        required=True
    )

    def validate_run_id(self, value):
        """Ensure run_id exists in the database"""
        if not Run.objects.filter(run_id=value).exists():
            raise serializers.ValidationError("Invalid run_id: Run does not exist")
        return value


class StorageSerializer(serializers.ModelSerializer):
    """Serializer for Storage model"""

    run_id = serializers.UUIDField(source="run.run_id", read_only=True)  # ✅ Ensures run_id compatibility
    upload_storage_location = serializers.SerializerMethodField()
    output_storage_location = serializers.SerializerMethodField()

    class Meta:
        model = Storage
        fields = [
            "storage_id",
            "user",
            "run_id",
            "file_id",
            "upload_storage_location",
            "output_storage_location",
        ]

    def get_upload_storage_location(self, obj):
        return get_logical_path(obj.upload_storage_location)

    def get_output_storage_location(self, obj):
        return get_logical_path(obj.output_storage_location)

class MetadataSerializer(serializers.ModelSerializer):
    """Serializer for Metadata model"""

    file = serializers.PrimaryKeyRelatedField(queryset=File.objects.all())
    storage = serializers.PrimaryKeyRelatedField(queryset=Storage.objects.all())

    class Meta:
        model = Metadata
        fields = [
            "metadata_id",
            "file",
            "storage",
            "file_size",
            "created_at",
            "updated_at",
            "deleted_at",
            "format",
            "title",
            "author",
            "subject",
            "keywords",
            "creator",
            "producer",
            "creationdate",
            "moddate",
            "trapped",
            "encryption",
            "page_count",
            "is_encrypted",
            "pdf_version",
            "fonts",
            "creator_pdf",  # Creator (PDF Metadata)
            "creationdate_pdf",  # Creationdate (PDF Metadata)
            "moddate_pdf",  # Moddate (PDF Metadata)
            "page_rotation",
            "custom_metadata",  # Custom Metadata
            "metadata_stream",
            "tagged",
            "userproperties",
            "suspects",
            "form",
            "javascript",
            "pages",
            "encrypted",
            "page_size",
            "optimized",
            "category",
            "content_status",
            "revision",
            "last_modified_by",
            "pdfminer_info",
            "word_count",
        ]


class WebhookSerializer(serializers.ModelSerializer):
    """Serializer for Webhook model"""

    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())

    class Meta:
        model = Webhook
        fields = ["id", "user", "webhook_url", "secret_key"]
