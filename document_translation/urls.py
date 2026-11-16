from django.urls import path
from .views import SubmitTranslationAPIView, CheckTranslationStatusAPIView, health_check
from .task_statuses import TranslationTaskStatusView, TranslationFileDownloadView  # Import new view


from .views import (
    TranslationRunSummaryView,
    TranslatedFilesByRunView,
    TranslationFileInsightView,
    TranslationHistoryView,
    SupportedLanguagesView,
    TranslationStorageLocationsView,
    TranslationClientSummaryView,
    TranslationProjectSummaryView,
    TranslationLanguageSummaryView,
    health_check,
)

urlpatterns = [
    # Translation actions
    path("submit-translation/", SubmitTranslationAPIView.as_view(), name="submit-translation"),
    path("check-translation-status/", TranslationTaskStatusView.as_view(), name="check-translation-status"),
    path("download-file/", TranslationFileDownloadView.as_view(), name="download-file"),

    # Translation insights (run/file-level)
    path("run-summary/", TranslationRunSummaryView.as_view(), name="translation-run-summary"),
    path("files/", TranslatedFilesByRunView.as_view(), name="translated-files-by-run"),
    path("insight/", TranslationFileInsightView.as_view(), name="translation-file-insight"),
    path("history/", TranslationHistoryView.as_view(), name="translation-history"),
    path("languages/", SupportedLanguagesView.as_view(), name="supported-languages"),
    path("storage-locations/", TranslationStorageLocationsView.as_view(), name="translation-storage-locations"),

    # Aggregation endpoints
    path("client-summary/", TranslationClientSummaryView.as_view(), name="translation-client-summary"),
    path("project-summary/", TranslationProjectSummaryView.as_view(), name="translation-project-summary"),
    path("language-summary/", TranslationLanguageSummaryView.as_view(), name="translation-language-summary"),

    # Health
    path("health/", health_check, name="health_check"),
]




'''
urlpatterns = [
    path("submit-translation/", SubmitTranslationAPIView.as_view(), name="submit-translation"),
    path("check-translation-status/", TranslationTaskStatusView.as_view(), name="check-translation-status"),
    path("download-file/", TranslationFileDownloadView.as_view(), name="download-file"),  # New file download endpoint
    # Health Checker
    path("health/", health_check, name="health_check"),
    ]
'''
