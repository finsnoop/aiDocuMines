from rest_framework import serializers
from .models import (
    Folder, FileFolderLink, EffectiveAccess, FileVersion,
    UserFileHide, UserFolderHide,
    FileColorTag, FolderColorTag, FileAlias, FolderAlias,
)
from core.models import File
from core.utils import get_logical_path
from django.contrib.auth import get_user_model

User = get_user_model()


class FileColorTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileColorTag
        fields = ['id', 'color', 'created_at', 'updated_at']


class FolderColorTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = FolderColorTag
        fields = ['id', 'color', 'created_at', 'updated_at']


class FileAliasSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = FileAlias
        fields = ['id', 'file_link', 'alias_name', 'created_by', 'created_by_email', 'created_at']
        read_only_fields = ['created_by', 'created_at']


class FolderAliasSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = FolderAlias
        fields = ['id', 'folder', 'alias_name', 'created_by', 'created_by_email', 'created_at']
        read_only_fields = ['created_by', 'created_at']


class FolderSerializer(serializers.ModelSerializer):
    subfolders = serializers.StringRelatedField(many=True, read_only=True)
    color_tag = FolderColorTagSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Folder
        fields = "__all__"


class FileFolderLinkSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source='file.filename', read_only=True)
    file_id = serializers.IntegerField(source='file.id', read_only=True)
    file_size = serializers.IntegerField(source='file.file_size', read_only=True)
    file_type = serializers.CharField(source='file.file_type', read_only=True)
    created_at = serializers.DateTimeField(source='file.created_at', read_only=True)
    updated_at = serializers.DateTimeField(source='file.updated_at', read_only=True)
    shared_with = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    color_tag = FileColorTagSerializer(read_only=True, allow_null=True)
    aliases = FileAliasSerializer(many=True, read_only=True)

    class Meta:
        model = FileFolderLink
        fields = [
            'id', 'file_id', 'file_name', 'file_size', 'file_type',
            'folder', 'is_trashed', 'is_shared', 'password_protected',
            'password_hint', 'shared_with', 'created_at', 'updated_at',
            'download_url', 'color_tag', 'aliases',
        ]

    def get_download_url(self, obj):
        """Return a tenant-safe logical path (never the raw server filesystem path)."""
        if obj.file:
            return get_logical_path(obj.file.filepath, obj.file.filename)
        return None

    def get_shared_with(self, obj):
        access_entries = obj.access_entries.select_related('user').all()
        return [
            {
                "user_id": entry.user.id,
                "email": entry.user.email,
                "can_read": entry.can_read,
                "can_write": entry.can_write,
                "can_delete": entry.can_delete,
                "can_share": entry.can_share,
            }
            for entry in access_entries if entry.user
        ]


class FileSerializer(serializers.ModelSerializer):
    folder_link = FileFolderLinkSerializer(read_only=True)
    filepath = serializers.SerializerMethodField()

    class Meta:
        model = File
        fields = [
            'id', 'filename', 'filepath', 'file_size',
            'file_type', 'status', 'folder_link',
        ]

    def get_filepath(self, obj):
        return get_logical_path(obj.filepath, obj.filename)


class EffectiveAccessSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = EffectiveAccess
        fields = "__all__"


class FileVersionSerializer(serializers.ModelSerializer):
    uploaded_by_email = serializers.EmailField(source='uploaded_by.email', read_only=True)
    file_path = serializers.SerializerMethodField()

    class Meta:
        model = FileVersion
        fields = [
            'version_number',
            'file_path',
            'uploaded_at',
            'uploaded_by_email',
        ]

    def get_file_path(self, obj):
        return get_logical_path(obj.file_path)



class RecursiveFolderSerializer(serializers.ModelSerializer):
    subfolders = serializers.SerializerMethodField()
    files      = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    color_tag = FolderColorTagSerializer(read_only=True, allow_null=True)

    class Meta:
        model  = Folder
        fields = [
            "id", "name", "project_id", "project_name", "service_id", "parent",
            "created_at", "is_trashed", "is_protected", "subfolders", "files",
            "color_tag",
        ]

    # ──────────────────────────────────────────────────────────
    def get_subfolders(self, obj):
        include_trashed = self.context.get("include_trashed", False)
        request = self.context.get("request")
        children = obj.subfolders.all()
        if not include_trashed:
            children = children.filter(is_trashed=False)
            if request and request.user.is_authenticated:
                hidden_ids = UserFolderHide.objects.filter(
                    user=request.user, folder__in=children
                ).values_list("folder_id", flat=True)
                children = children.exclude(id__in=hidden_ids)
        children = children.order_by("name")
        return RecursiveFolderSerializer(
            children, many=True, context=self.context
        ).data

    def get_files(self, obj):
        if not self.context.get("include_files", True):
            return []

        include_trashed = self.context.get("include_trashed", False)
        request = self.context.get("request")
        links = obj.files.all().select_related("file")
        if include_trashed:
            trashed_links = links.filter(is_trashed=True)
            hidden_ids = set()
            if request and request.user.is_authenticated:
                hidden_ids = set(UserFileHide.objects.filter(
                    user=request.user, file_link__in=links
                ).values_list("file_link_id", flat=True))
                hidden_links = links.filter(id__in=hidden_ids)
                combined = list(trashed_links) + list(hidden_links)
                data = FileFolderLinkSerializer(combined, many=True, context=self.context).data
                for item in data:
                    if item.get("id") in hidden_ids:
                        item["hidden_by_me"] = True
                return data
            return FileFolderLinkSerializer(trashed_links, many=True, context=self.context).data
        else:
            if request and request.user.is_authenticated:
                hidden_ids = UserFileHide.objects.filter(
                    user=request.user, file_link__in=links
                ).values_list("file_link_id", flat=True)
                links = links.exclude(id__in=hidden_ids)
            links = links.filter(is_trashed=False)
            return FileFolderLinkSerializer(links, many=True, context=self.context).data

    def get_project_name(self, obj):
        try:
            from core.models import Project
            project = Project.objects.filter(id=obj.project_id).first()
            if project:
                return project.project_name
        except Exception:
            pass
        return obj.project_id  # fallback to UUID



'''
class RecursiveFolderSerializer(serializers.ModelSerializer):
    subfolders = serializers.SerializerMethodField()
    files = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    class Meta:
        model = Folder
        fields = [
            'id', 'name', 'project_id', 'project_name', 'service_id', 'parent',
            'created_at', 'subfolders', 'files', 'is_trashed'
        ]

    def get_subfolders(self, obj):
        # Use reverse relation 'subfolders' or 'children' depending on your related_name
        children = obj.subfolders.all().order_by("name")  # adjust if your related_name differs
        return RecursiveFolderSerializer(children, many=True, context=self.context).data

    def get_files(self, obj):
        include = self.context.get("include_files", True)
        if not include:
            return []

        # Avoid returning trashed or unrelated files
        links = obj.filefolderlink_set.filter(is_trashed=False).select_related("file")
        return FileFolderLinkSerializer(links, many=True).data

    def get_project_name(self, obj):
        try:
            from core.models import Project
            project = Project.objects.filter(id=obj.project_id).first()
            if project:
                return project.project_name
        except Exception:
            pass
        return obj.project_id  # fallback to UUID
'''
