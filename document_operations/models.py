from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from core.models import File
import uuid

User = get_user_model()

class Folder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="subfolders")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    project_id = models.CharField(max_length=255, db_index=True)
    service_id = models.CharField(max_length=255, db_index=True)
    is_trashed = models.BooleanField(default=False)
    is_protected = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({'trashed' if self.is_trashed else 'active'})"


class FileFolderLink(models.Model):
    #file = models.OneToOneField(File, on_delete=models.CASCADE, related_name="folder_link")
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name="folder_links")
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name="files")
    is_trashed = models.BooleanField(default=False)
    is_protected = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=False)
    shared_with = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="shared_files")
    password_protected = models.BooleanField(default=False)
    password_hint = models.CharField(max_length=255, null=True, blank=True)
    password_hash = models.CharField(max_length=128, blank=True, default="")
    is_public = models.BooleanField(default=False)  # For public shared link
    public_token = models.CharField(max_length=64, unique=True, null=True, blank=True)  # Unique token for access
    shared_link_created_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.file.filename} in {self.folder.name}"


class EffectiveAccess(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.CASCADE)
    file = models.ForeignKey(File, null=True, blank=True, on_delete=models.CASCADE)
    can_rename = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_move = models.BooleanField(default=False)
    can_download = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)
    can_zip = models.BooleanField(default=False)
    can_protect = models.BooleanField(default=False)
    can_duplicate = models.BooleanField(default=False)
    can_restore = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "folder", "file")


class FileVersion(models.Model):
    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name='versions')
    version_number = models.PositiveIntegerField()
    file_path = models.TextField()  # absolute or relative path
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    class Meta:
        unique_together = ('file', 'version_number')
        ordering = ['-version_number']

    def __str__(self):
        return f"{self.file.filename} - v{self.version_number}"


class FileAuditLog(models.Model):
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('deleted', 'Deleted'),
        ('renamed', 'Renamed'),
        ('downloaded', 'Downloaded'),
        ('shared', 'Shared'),
        ('unshared', 'Unshared'),
        ('trashed', 'Trashed'),
        ('restored', 'Restored'),
        ('version_restored', 'Version Restored'),
        ('public_link', 'Public Link Generated'),
        ('password_set', 'Password Protected'),
        ('previewed', 'Previewed'),
        ('access_updated', 'Access Level Updated'),
    ]

    file = models.ForeignKey(File, on_delete=models.CASCADE, related_name='audit_logs')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)

    notes = models.TextField(blank=True, null=True)  # Optional human-readable explanation
    extra = models.JSONField(blank=True, null=True)  # Structured metadata for API/debugging/audit

    def __str__(self):
        return f"{self.user.email if self.user else 'System'} - {self.file.filename} - {self.action} at {self.timestamp}"




class FileAccessEntry(models.Model):
    """
    Granular access entry: maps a user or group to a FileFolderLink with detailed permissions.
    Includes optional expiration and granter tracking.
    """
    file_link = models.ForeignKey(FileFolderLink, on_delete=models.CASCADE, related_name="access_entries")

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.CASCADE)

    can_read = models.BooleanField(default=True)
    can_write = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)

    expires_at = models.DateTimeField(null=True, blank=True)

    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="access_grants")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["file_link", "user"], name="unique_file_user_access"),
            models.UniqueConstraint(fields=["file_link", "group"], name="unique_file_group_access"),
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, group__isnull=True) |
                    models.Q(user__isnull=True, group__isnull=False)
                ),
                name="only_user_or_group"
            )
        ]

    def __str__(self):
        target = self.user.email if self.user else f"[Group] {self.group.name}"
        return f"{target} -> {self.file_link.file.filename} (R:{self.can_read}, W:{self.can_write})"


class UserFileHide(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    file_link = models.ForeignKey(FileFolderLink, on_delete=models.CASCADE, related_name='user_hides')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'file_link')

    def __str__(self):
        return f"{self.user.email} hid file_link {self.file_link.id}"


class UserFolderHide(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='user_hides')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'folder')

    def __str__(self):
        return f"{self.user.email} hid folder {self.folder.id}"


COLOR_CHOICES = [
    ('red', 'Red'),
    ('green', 'Green'),
    ('blue', 'Blue'),
    ('orange', 'Orange'),
    ('purple', 'Purple'),
    ('grey', 'Grey'),
    ('yellow', 'Yellow'),
]


class FileColorTag(models.Model):
    file_link = models.OneToOneField(FileFolderLink, on_delete=models.CASCADE, related_name='color_tag')
    color = models.CharField(max_length=20, choices=COLOR_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.file_link.file.filename} -> {self.color}"


class FolderColorTag(models.Model):
    folder = models.OneToOneField(Folder, on_delete=models.CASCADE, related_name='color_tag')
    color = models.CharField(max_length=20, choices=COLOR_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.folder.name} -> {self.color}"


class FileAlias(models.Model):
    file_link = models.ForeignKey(FileFolderLink, on_delete=models.CASCADE, related_name='aliases')
    alias_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('file_link', 'alias_name')

    def __str__(self):
        return f"{self.alias_name} -> {self.file_link.file.filename}"


class FolderAlias(models.Model):
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='aliases')
    alias_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('folder', 'alias_name')

    def __str__(self):
        return f"{self.alias_name} -> {self.folder.name}"

