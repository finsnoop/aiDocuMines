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

urlpatterns = [
    # ✅ File Upload Endpoint
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

    # Projects
    path("projects/", ProjectListView.as_view(), name="project-list"),
    path("projects/create/", ProjectCreateView.as_view(), name="project-create"),

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
