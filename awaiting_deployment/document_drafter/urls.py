# app_layout/urls.py

from django.urls import path, include    # → missing earlier
from rest_framework.routers import DefaultRouter    # → missing earlier

from .views import (
    UploadedDocumentViewSet,
    CatalogEntryViewSet,
    ChatHistoryViewSet,
    AskQuestionView,
    TaskStatusView,
    AllTaskStatusesView,
    GenerateClientPersonaView,
    GenerateProposalView,
    ClientPersonaListView,  # 🆕 good
    ProposalDraftViewSet,
    MatchPersonaToCatalogView,
    SuggestedSolutionListView,
    GenerateSuggestedSolutionsView,
    SignupView,
    FetchCredentialsView,
    AskSolutionQuestionView,
    DownloadProposalDocxView,
    NotificationViewSet,
    UploadIncomingRFPView,
    IncomingRFPListView,
    AskRFPQuestionView,
    IncomingRFPDetailView,
    RFPChatHistoryListView,
    RFPProcessingStatusView,
    GenerateRFPDocxView
    
)

router = DefaultRouter()
router.register(r'documents', UploadedDocumentViewSet, basename='uploaded-document')
router.register(r'catalog-entries', CatalogEntryViewSet, basename='catalog-entry')
router.register(r'chat-history', ChatHistoryViewSet, basename='chat-history')
router.register(r'proposal-drafts', ProposalDraftViewSet, basename='proposal-draft')

urlpatterns = [
    
    # API endpoints
    path('api/', include(router.urls)),
    
    # Document chat
    path('api/ask-question/', AskQuestionView.as_view(), name='ask-question'),
    
    # Solution chat
    path('api/ask-solution-question/', AskSolutionQuestionView.as_view(), name='ask-solution-question'),
    
    # Task status
    path('api/task-status/<int:document_id>/', TaskStatusView.as_view(), name='task-status'),
    path('api/task-statuses/', AllTaskStatusesView.as_view(), name='all-task-statuses'),
    
    # Client Personas
    path('api/generate-client-persona/', GenerateClientPersonaView.as_view(), name='generate-client-persona'),
    path('api/client-personas/', ClientPersonaListView.as_view(), name='client-personas'),  # 🆕 perfect
    
    # Proposal Drafts
    path('api/generate-proposal/', GenerateProposalView.as_view(), name='generate-proposal'),
    path("proposal-docx/<int:proposal_id>/", DownloadProposalDocxView.as_view(), name="download_proposal_docx"),
    
    # Suggested Solutions
    path('api/match-persona-to-catalog/', MatchPersonaToCatalogView.as_view(), name='match-persona-to-catalog'),
    path('api/suggested-solutions/', SuggestedSolutionListView.as_view(), name='suggested-solutions'),
    path('api/generate-suggested-solutions/', GenerateSuggestedSolutionsView.as_view(), name='generate-suggested-solutions'),
    
    # Notifications
    # Standard CRUD
    path('api/notifications/', NotificationViewSet.as_view({'get': 'list'}), name='notification-list'),
    path('api/notifications/<int:pk>/', NotificationViewSet.as_view({'get': 'retrieve'}), name='notification-detail'),
    path('api/notifications/create/', NotificationViewSet.as_view({'post': 'create'}), name='notification-create'),
    path('api/notifications/update/<int:pk>/', NotificationViewSet.as_view({'put': 'update'}), name='notification-update'),
    path('api/notifications/delete/<int:pk>/', NotificationViewSet.as_view({'delete': 'destroy'}), name='notification-delete'),
    
    # Read / Unread
    path('api/notifications/mark-as-read/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_read'}), name='notification-mark-as-read'),
    path('api/notifications/mark-as-unread/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_unread'}), name='notification-mark-as-unread'),
    path('api/notifications/mark-all-as-read/', NotificationViewSet.as_view({'post': 'mark_all_as_read'}), name='notification-mark-all-as-read'),
    path('api/notifications/mark-all-as-unread/', NotificationViewSet.as_view({'post': 'mark_all_as_unread'}), name='notification-mark-all-as-unread'),
    
    # Archived
    path('api/notifications/mark-as-archived/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_archived'}), name='notification-mark-as-archived'),
    path('api/notifications/mark-as-unarchived/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_unarchived'}), name='notification-mark-as-unarchived'),
    path('api/notifications/mark-all-as-archived/', NotificationViewSet.as_view({'post': 'mark_all_as_archived'}), name='notification-mark-all-as-archived'),
    path('api/notifications/mark-all-as-unarchived/', NotificationViewSet.as_view({'post': 'mark_all_as_unarchived'}), name='notification-mark-all-as-unarchived'),
    
    # Important
    path('api/notifications/mark-as-important/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_important'}), name='notification-mark-as-important'),
    path('api/notifications/mark-as-not-important/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_not_important'}), name='notification-mark-as-not-important'),
    path('api/notifications/mark-all-as-important/', NotificationViewSet.as_view({'post': 'mark_all_as_important'}), name='notification-mark-all-as-important'),
    path('api/notifications/mark-all-as-not-important/', NotificationViewSet.as_view({'post': 'mark_all_as_not_important'}), name='notification-mark-all-as-not-important'),
    
    # Deleted
    path('api/notifications/mark-as-deleted/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_deleted'}), name='notification-mark-as-deleted'),
    path('api/notifications/mark-as-not-deleted/<int:pk>/', NotificationViewSet.as_view({'post': 'mark_as_not_deleted'}), name='notification-mark-as-not-deleted'),
    path('api/notifications/mark-all-as-deleted/', NotificationViewSet.as_view({'post': 'mark_all_as_deleted'}), name='notification-mark-all-as-deleted'),
    path('api/notifications/mark-all-as-not-deleted/', NotificationViewSet.as_view({'post': 'mark_all_as_not_deleted'}), name='notification-mark-all-as-not-deleted'),
    
    # Incoming RFPs
    path('api/rfps/upload/', UploadIncomingRFPView.as_view(), name='upload-incoming-rfp'),
    path('api/rfps/', IncomingRFPListView.as_view(), name='incoming-rfp-list'),
    path('api/rfps/ask/', AskRFPQuestionView.as_view(), name='ask-rfp-question'),
    path('api/rfps/<int:pk>/', IncomingRFPDetailView.as_view(), name='rfp-detail'),       # GET / PATCH
    path('api/rfps/<int:rfp_id>/chats/', RFPChatHistoryListView.as_view(), name='rfp-chats'),  # Q&A history 
    path('api/rfps/status/', RFPProcessingStatusView.as_view(), name='rfp-status'),
    path('api/generate-docx/', GenerateRFPDocxView.as_view(), name='generate-docx'),
    
    # Auth
    path('api/signup/', SignupView.as_view(), name='signup'),
    path('api/fetch-credentials/', FetchCredentialsView.as_view(), name='fetch-credentials'),

]

