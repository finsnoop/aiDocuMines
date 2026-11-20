from django.urls import path
from .views_spend import AISpendLimitView, AISpendView
from .views_layout import DashboardLayoutView
from .views_intelligence import (BusinessDashboardView, IntelligenceRunsView, IntelligenceSourcesView,
                                IntelligenceReviewView, IntelligenceRulesView, IntelligenceTokenUsageView,
                                IntelligenceCompareView)
from .views import (
    health,
    HomeDashAllView,
    HomeDashSectionView,
    HomeDashSnapshotView,
    # Optional extras – only include if you implemented them in views.py
    HomeDashCardsView,
    HomeDashTimeSeriesView,
    HomeDashTopFilesView,
    HomeDashTopSearchesView,
)

from .views_agents import AgentOperationsView, AIBudgetView, ModelPriceView

urlpatterns = [
    path('business/', BusinessDashboardView.as_view()),
    path('intelligence/runs/', IntelligenceRunsView.as_view()),
    path('intelligence/sources/', IntelligenceSourcesView.as_view()),
    path('intelligence/review/<int:file_id>/', IntelligenceReviewView.as_view()),
    path('intelligence/rules/', IntelligenceRulesView.as_view()),
    path('intelligence/tokens/', IntelligenceTokenUsageView.as_view()),
    path('intelligence/compare/', IntelligenceCompareView.as_view()),
    path('agents/', AgentOperationsView.as_view()),
    path('agents/budget/', AIBudgetView.as_view()),
    path('ai-spend/', AISpendView.as_view()),
    path('ai-spend/limits/', AISpendLimitView.as_view()),
    path('ai-spend/limits/<int:pk>/', AISpendLimitView.as_view()),
    path('agents/prices/', ModelPriceView.as_view()),
    path("health/", health, name="home_analytics_health"),

    # Overview (alias of “me/” for nicer API)
    path("dashboard-layout/", DashboardLayoutView.as_view(), name="home-dash-layout"),
    path("overview/", HomeDashAllView.as_view(), name="home-dash-overview"),
    path("me/", HomeDashAllView.as_view(), name="home-dash-me"),

    # Snapshot – put BEFORE any catch-all patterns
    path("me/snapshots/latest/", HomeDashSnapshotView.as_view(), name="home-dash-snapshot-latest"),

    # Sectioned analytics (matches your curl: /section/<key>/)
    path("section/<str:section>/", HomeDashSectionView.as_view(), name="home-dash-section"),

    # Optional extras (only if you added these views)
    path("cards/", HomeDashCardsView.as_view(), name="home-dash-cards"),
    path("timeseries/", HomeDashTimeSeriesView.as_view(), name="home-dash-timeseries"),
    path("top/files/", HomeDashTopFilesView.as_view(), name="home-dash-top-files"),
    path("top/searches/", HomeDashTopSearchesView.as_view(), name="home-dash-top-searches"),
]

