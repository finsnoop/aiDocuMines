from django.urls import path
from .views import FileUploadView, MetadataView, BulkFolderUploadView, FileDownloadView, health_check  # UniversalTaskStatusView,
from .task_statuses import UploadStatusView, MetadataStatusView # MetadataExtractionTriggerView
from core.views import AssociateTopicToFileView


from .views import (
    FileInsightView,
    RunSummaryView,
    ClientSummaryView,
    StorageLocationsView,
    ProjectSummaryView,
    ServiceSummaryView,
    ProjectListView,
    ProjectCreateView,
    UserPreferenceView,
)

from core.views_bulk import (
    BulkServiceCatalogView,
    BulkJobSubmitView,
    BulkJobStatusView,
    BulkJobResultsView,
    BulkJobListView,
    BulkJobCancelView,
    BulkJobRetryView,
    BulkJobDownloadView,
)


from core.onlyoffice_views import (
    OnlyOfficeConvertView,
    OnlyOfficeCommandView,
    OnlyOfficeSignedDownloadView,
    OnlyOfficeCallbackView,
    OnlyOfficeEditorConfigView,
    OnlyOfficeCreateDocxView,
    OnlyOfficeDocServerUrlView
)

from .views_matters import (MatterCreateView, MatterDetailView, MatterDocumentsView, MatterJobDownloadView,
                            MatterJobsView, MatterListView, MatterMembersView, MatterPeopleView, MatterRunView,
                            MatterUploadView)

from .views_assistant import AssistantAskView, AssistantStatusView
from .views_backup import BackupDetailView, BackupDownloadView, BackupListView, BackupRestoreView
urlpatterns = [
    # ✅ File Upload Endpoint
    # Matters (a deal, case or request: documents, workflows, results and team)
    # Context assistant (chat with a file, folder, matter or project)
    path("assistant/ask/", AssistantAskView.as_view(), name="assistant-ask"),
    path("assistant/status/", AssistantStatusView.as_view(), name="assistant-status"),
    # Platform database backups (platform super-admins)
    path("backups/", BackupListView.as_view(), name="backup-list"),
    path("backups/<str:name>/", BackupDetailView.as_view(), name="backup-detail"),
    path("backups/<str:name>/download/", BackupDownloadView.as_view(), name="backup-download"),
    path("backups/<str:name>/restore/", BackupRestoreView.as_view(), name="backup-restore"),
    path("projects/", MatterListView.as_view(), name="matter-list"),
    path("projects/create/", MatterCreateView.as_view(), name="matter-create"),
    path("projects/<uuid:matter_id>/", MatterDetailView.as_view(), name="matter-detail"),
    path("projects/<uuid:matter_id>/members/", MatterMembersView.as_view(), name="matter-members"),
    path("projects/<uuid:matter_id>/members/<int:user_id>/", MatterMembersView.as_view(), name="matter-member"),
    path("projects/<uuid:matter_id>/upload/", MatterUploadView.as_view(), name="matter-upload"),
    path("projects/<uuid:matter_id>/documents/", MatterDocumentsView.as_view(), name="matter-documents"),
    path("projects/<uuid:matter_id>/jobs/", MatterJobsView.as_view(), name="matter-jobs"),
    path("projects/<uuid:matter_id>/run/", MatterRunView.as_view(), name="matter-run"),
    path("projects/<uuid:matter_id>/people/", MatterPeopleView.as_view(), name="matter-people"),
    path("projects/<uuid:matter_id>/jobs/<uuid:job_id>/download/", MatterJobDownloadView.as_view(), name="matter-job-download"),
    path("upload/", FileUploadView.as_view(), name="file-upload"),

    # ✅ Task Status (Universal)
    # path("task-status/", UniversalTaskStatusView.as_view(), name="universal-task-status"),

    # ✅ Bulk Folder Upload Endpoint
    path("upload/folder/", BulkFolderUploadView.as_view(), name="bulk-folder-upload"),  # ✅ New path

    # ✅ Upload Processing Status
    path("upload-status/", UploadStatusView.as_view(), name="upload-status"),

    # ✅ File Download
    path("download/", FileDownloadView.as_view(), name="file-download"),

    # ✅ Metadata for a Specific File
    path("metadata/", MetadataView.as_view(), name="metadata"),

    # ✅ Metadata Extraction Trigger
    # path("metadata/extract/", MetadataExtractionTriggerView.as_view(), name="metadata-extract"),

    # ✅ All Metadata Records
    path("metadata-status/", MetadataStatusView.as_view(), name="metadata-status"),
    
    # For document interrogation
    path('files/<int:file_id>/associate-topic/', AssociateTopicToFileView.as_view(), name='associate-topic'),


    # ✅ File Insight by file_id
    path("file-insight/", FileInsightView.as_view(), name="file-insight"),

    # ✅ Run summary by run_id
    path("run-summary/", RunSummaryView.as_view(), name="run-summary"),

    # ✅ Dashboard for logged-in client
    path("client-summary/", ClientSummaryView.as_view(), name="client-summary"),

    # ✅ File storage mapping
    path("storage-locations/", StorageLocationsView.as_view(), name="storage-locations"),

    # ✅ Filter files by project_id
    path("project-summary/", ProjectSummaryView.as_view(), name="project-summary"),

    # ✅ Filter files by service_id
    path("service-summary/", ServiceSummaryView.as_view(), name="service-summary"),



    # Health Checker
    path("health/", health_check, name="health_check"),

    # Bulk Processing
    path("bulk/catalog/", BulkServiceCatalogView.as_view(), name="bulk-catalog"),
    path("bulk/submit/", BulkJobSubmitView.as_view(), name="bulk-submit"),
    path("bulk/list/", BulkJobListView.as_view(), name="bulk-list"),
    path("bulk/<str:job_id>/status/", BulkJobStatusView.as_view(), name="bulk-status"),
    path("bulk/<str:job_id>/results/", BulkJobResultsView.as_view(), name="bulk-results"),
    path("bulk/<str:job_id>/cancel/", BulkJobCancelView.as_view(), name="bulk-cancel"),
    path("bulk/<str:job_id>/retry/", BulkJobRetryView.as_view(), name="bulk-retry"),
    path("bulk/<str:job_id>/download/", BulkJobDownloadView.as_view(), name="bulk-download"),

    # Projects: core/projects/ is served by the Matters API above (same paths, richer model).

    # User preferences
    path("preferences/", UserPreferenceView.as_view(), name="user-preferences"),
    
]

urlpatterns += [
    path("onlyoffice/convert/", OnlyOfficeConvertView.as_view(), name="onlyoffice-convert"),
    path("onlyoffice/command/", OnlyOfficeCommandView.as_view(), name="onlyoffice-command"),
    path("onlyoffice/signed-download/", OnlyOfficeSignedDownloadView.as_view(), name="onlyoffice-signed-download"),
    path("onlyoffice/callback/", OnlyOfficeCallbackView.as_view(), name="onlyoffice-callback"),
    path("onlyoffice/editor-config/", OnlyOfficeEditorConfigView.as_view(), name="onlyoffice-editor-config"),
    path("onlyoffice/create-docx/", OnlyOfficeCreateDocxView.as_view(), name="onlyoffice-create-docx"),
    path("onlyoffice/docserver-url/", OnlyOfficeDocServerUrlView.as_view(), name="onlyoffice-docserver-url"),
    ]
