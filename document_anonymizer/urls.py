from django.urls import path
from .views import (
    SubmitAnonymizationAPIView,
    SubmitDeAnonymizationAPIView,
    DownloadAnonymizedFileAPIView,
    DownloadDeAnonymizedFileAPIView,
    StructuredBlocksView,
    SummarizeBlocksView,
    EditBlockView,
    ExportBlocksView,
    DownloadStructuredMarkdownView,
    DownloadStructuredHTMLView,
    DownloadStructuredTextView,
    DownloadStructuredJSONView,
    DocumentRiskScoreView,               # ✅ NEW
    DocumentRiskScoreResultView,        # ✅ NEW
    DownloadStructuredDocxView,
    DownloadRedactionReadyJSONView,
    AnonymizationStatsView,
    AnonymizationStatsResultView,
    AnonymizationStatsHistoryView,
    AnonymizationInsightsView,
    RiskBucketDocumentsView,
    SupportedEntitiesView,   # ← add this
    CheckAnonymizationResultsView,
    health_check
)
from .task_statuses import AnonymizationTaskStatusView

urlpatterns = [
    # Anonymization
    path("submit-anonymization/", SubmitAnonymizationAPIView.as_view(), name="submit-anonymization"),
    path("check-anonymization-status/", AnonymizationTaskStatusView.as_view(), name="check-anonymization-status"),
    path("download-anonymized-file/", DownloadAnonymizedFileAPIView.as_view(), name="download-anonymized-file"),

    # Structured Block Utilities (NEW)
    path("structured-blocks/", StructuredBlocksView.as_view(), name="structured-blocks"),
    path("summarize-blocks/", SummarizeBlocksView.as_view(), name="summarize-blocks"),
    path("edit-block/", EditBlockView.as_view(), name="edit-block"),
    path("export-blocks/", ExportBlocksView.as_view(), name="export-blocks"),

    # De-Anonymization
    path("submit-deanonymization/", SubmitDeAnonymizationAPIView.as_view(), name="submit-deanonymization"),
    path("check-deanonymization-status/", AnonymizationTaskStatusView.as_view(), name="check-deanonymization-status"),
    path("download-deanonymized-file/", DownloadDeAnonymizedFileAPIView.as_view(), name="download-deanonymized-file"),

    # Structured Downloads
    path("download-structured-markdown/", DownloadStructuredMarkdownView.as_view(), name="download-structured-markdown"),
    path("download-structured-html/", DownloadStructuredHTMLView.as_view(), name="download-structured-html"),
    path("download-structured-text/", DownloadStructuredTextView.as_view(), name="download-structured-text"),
    path("download-structured-json/", DownloadStructuredJSONView.as_view(), name="download-structured-json"),
    path("download-structured-docx/", DownloadStructuredDocxView.as_view(), name="download-structured-docx"),

    # Download Masks json file
    path("download-redaction-json/", DownloadRedactionReadyJSONView.as_view(), name="download-redaction-json"),

    # Risk Score
    path("risk-score/", DocumentRiskScoreView.as_view(), name="risk-score"),
    path("risk-score-result/", DocumentRiskScoreResultView.as_view(), name="risk-score-result"),

    # Anonymization Stats
    path("anonymization-stats/", AnonymizationStatsView.as_view(), name="anonymization-stats"),
    path("anonymization-stats-result/", AnonymizationStatsResultView.as_view(), name="anonymization-stats-result"),
    path("anonymization-stats-history/", AnonymizationStatsHistoryView.as_view(), name="anonymization-stats-history"),

    # ✅ NEW: Anonymization Insights
    path("anonymization-insights/", AnonymizationInsightsView.as_view(), name="anonymization-insights"),
    path("risk-bucket-documents/", RiskBucketDocumentsView.as_view(), name="risk-bucket-documents"),


    # urlpatterns (add near the insights / stats routes)
    path("supported-entities/", SupportedEntitiesView.as_view(), name="supported-entities"),

    # Batch check results
    path("check-results/", CheckAnonymizationResultsView.as_view(), name="check-anonymization-results"),

    # Health
    path("health/", health_check, name="health_check"),
]

