from django.urls import path

from . import views

urlpatterns = [
    path("runs/", views.RunsView.as_view(), name="ai-eval-runs"),
    path("runs/<uuid:pk>/", views.RunDetailView.as_view(), name="ai-eval-run"),
    path("summary/", views.SummaryView.as_view(), name="ai-eval-summary"),
]
