from django.urls import path
from .views import (
    FolderDetailView, FileDetailView,
    RenameFileView, RenameFolderView,
    TrashFilesView, MoveFilesView,
    DeleteFileView, DeleteFolderView,
    DuplicateFileView, CopyFileView,
    ZipFilesView, ProtectFileView,
    RestoreFileView, RestoreFolderView,
    ListFileVersionsView, RestoreFileVersionView,
    ShareFileView, UnshareFileView, SharedFilesView,
    FilePreviewView, FileAuditLogView,
    PublicSharedFileView, FolderListView,
    FolderCreateView, TrashSingleFileView, TrashFolderView, FolderListCreateView,
    ShareWithGroupView, SetAccessLevelView, GrantPublicLinkView,
    CreateChildFolderView, UploadFileToFolderView,
    HideFileView, UnhideFileView,
    HideFolderView, UnhideFolderView,
    BulkTrashFoldersView, BulkRestoreFoldersView, BulkDeleteFoldersView,
    SharedTreeView,
    FileColorTagView, FolderColorTagView,
    FileAliasView, FileAliasDetailView, FolderAliasView, FolderAliasDetailView,
)

from .views_table import DocumentFacetsView, DocumentTableView

urlpatterns = [
    # 📁 FOLDER OPERATIONS
    # path("folders/", FolderListView.as_view(), name="folder_list"),
    # path("folders/", FolderCreateView.as_view(), name="create-folder"),
    # Flat, paginated document table (all accessible documents)
    path("table/", DocumentTableView.as_view(), name="document_table"),
    path("table/facets/", DocumentFacetsView.as_view(), name="document_table_facets"),
    path("folders/", FolderListCreateView.as_view(), name="folder_list_create"),
    path("folders/create-child/", CreateChildFolderView.as_view(), name="folder_create_child"),
    path("files/<int:pk>/trash/", TrashSingleFileView.as_view(), name="trash-file"),
    path("folders/<uuid:pk>/", FolderDetailView.as_view(), name="folder_detail"),
    path("folders/<uuid:pk>/rename/", RenameFolderView.as_view(), name="folder_rename"),
    path("folders/<uuid:pk>/trash/", TrashFolderView.as_view(), name="folder_trash"),
    path("folders/<uuid:pk>/delete/", DeleteFolderView.as_view(), name="folder_delete"),
    path("folders/<uuid:pk>/restore/", RestoreFolderView.as_view(), name="folder_restore"),

    # 📄 FILE OPERATIONS
    path("files/upload-to-folder/", UploadFileToFolderView.as_view(), name="file_upload_to_folder"),
    path("files/<int:pk>/", FileDetailView.as_view(), name="file_detail"),
    path("files/<int:pk>/rename/", RenameFileView.as_view(), name="file_rename"),
    path("files/<int:pk>/delete/", DeleteFileView.as_view(), name="file_delete"),
    path("files/<int:pk>/duplicate/", DuplicateFileView.as_view(), name="file_duplicate"),
    path("files/<int:pk>/copy/", CopyFileView.as_view(), name="file_copy"),
    path("files/<int:pk>/protect/", ProtectFileView.as_view(), name="file_protect"),
    path("files/<int:pk>/restore/", RestoreFileView.as_view(), name="file_restore"),

    # 🗂️ BULK & UTILITIES
    path("files/trash/", TrashFilesView.as_view(), name="file_bulk_trash"),
    path("files/move/", MoveFilesView.as_view(), name="file_bulk_move"),
    path("files/zip/", ZipFilesView.as_view(), name="file_zip"),

    # 📜 FILE VERSIONING
    path("files/<int:pk>/versions/", ListFileVersionsView.as_view(), name="file_versions_list"),
    path("files/<int:pk>/versions/<int:version_number>/restore/", RestoreFileVersionView.as_view(), name="file_version_restore"),

    # 🔗 FILE SHARING
    path("files/<int:pk>/share/", ShareFileView.as_view(), name="file_share"),
    path("files/<int:pk>/unshare/", UnshareFileView.as_view(), name="file_unshare"),
    path("files/shared/", SharedFilesView.as_view(), name="file_shared_with_me"),
    path("files/shared-tree/", SharedTreeView.as_view(), name="file_shared_tree"),

    # 👁️ FILE PREVIEW
    path("files/<int:pk>/preview/", FilePreviewView.as_view(), name="file_preview"),

    # 🕵️‍♂️ FILE ACTIVITY LOGS
    path("files/<int:pk>/audit/", FileAuditLogView.as_view(), name="file_audit_log"),

    # 🙈 HIDE / UNHIDE (per-user soft-delete for shared items)
    path("files/<int:pk>/hide/", HideFileView.as_view(), name="file_hide"),
    path("files/<int:pk>/unhide/", UnhideFileView.as_view(), name="file_unhide"),
    path("folders/<uuid:pk>/hide/", HideFolderView.as_view(), name="folder_hide"),
    path("folders/<uuid:pk>/unhide/", UnhideFolderView.as_view(), name="folder_unhide"),

    # 🗂️ BULK FOLDER CASCADE OPERATIONS
    path("folders/bulk-trash/", BulkTrashFoldersView.as_view(), name="folder_bulk_trash"),
    path("folders/bulk-restore/", BulkRestoreFoldersView.as_view(), name="folder_bulk_restore"),
    path("folders/bulk-delete/", BulkDeleteFoldersView.as_view(), name="folder_bulk_delete"),

    # 🌐 PUBLIC ACCESS (if enabled)
    path("public/share/<uuid:share_token>/", PublicSharedFileView.as_view(), name="public_file_access"),

    # EXTRA FILE SHARING
    path("files/<int:pk>/share/group/", ShareWithGroupView.as_view(), name="file_share_with_group"),
    path("files/<int:pk>/access-level/", SetAccessLevelView.as_view(), name="file_access_level_update"),
    path("files/<int:pk>/share/public/", GrantPublicLinkView.as_view(), name="file_grant_public_link"),

    # 🎨 COLOR TAGS
    path("files/<int:pk>/color/", FileColorTagView.as_view(), name="file_color"),
    path("folders/<uuid:pk>/color/", FolderColorTagView.as_view(), name="folder_color"),

    # 🔗 ALIASES
    path("files/<int:pk>/aliases/", FileAliasView.as_view(), name="file_aliases"),
    path("files/<int:pk>/aliases/<int:alias_id>/", FileAliasDetailView.as_view(), name="file_alias_detail"),
    path("folders/<uuid:pk>/aliases/", FolderAliasView.as_view(), name="folder_aliases"),
    path("folders/<uuid:pk>/aliases/<int:alias_id>/", FolderAliasDetailView.as_view(), name="folder_alias_detail"),

    ]

