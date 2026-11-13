from django.urls import path
from .views_layout import DashboardLayoutView
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

urlpatterns = [
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

