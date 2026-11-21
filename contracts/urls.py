from django.urls import path

from . import views, views_playbook as pv

urlpatterns = [
    path("contracts/", views.ContractListView.as_view(), name="contracts"),
    path("contracts/<uuid:pk>/", views.ContractDetailView.as_view(), name="contract"),
    path("contracts/<uuid:pk>/advance/", views.AdvanceView.as_view(), name="contract-advance"),
    path("intake/", views.IntakeView.as_view(), name="contract-intake"),
    path("approvals/mine/", views.MyApprovalsView.as_view(), name="contract-my-approvals"),
    path("approvals/<int:pk>/decide/", views.DecideView.as_view(), name="contract-approval-decide"),
    path("rules/", views.RulesView.as_view(), name="contract-rules"),
    path("rules/<int:pk>/", views.RulesView.as_view(), name="contract-rule"),
    path("obligations/", views.ObligationListView.as_view(), name="obligations"),
    path("obligations/<uuid:pk>/", views.ObligationDetailView.as_view(), name="obligation"),
    path("playbooks/", pv.PlaybookListView.as_view(), name="playbooks"),
    path("playbooks/<uuid:pk>/", pv.PlaybookDetailView.as_view(), name="playbook"),
    path("playbooks/<uuid:pk>/positions/", pv.PositionView.as_view(), name="playbook-positions"),
    path("playbooks/<uuid:pk>/positions/<int:position_id>/", pv.PositionView.as_view(), name="playbook-position"),
    path("reviews/", pv.ReviewStartView.as_view(), name="playbook-review-start"),
    path("reviews/<uuid:pk>/", pv.ReviewDetailView.as_view(), name="playbook-review"),
    path("contracts/<uuid:pk>/reviews/", pv.ContractReviewsView.as_view(), name="contract-reviews"),
]
