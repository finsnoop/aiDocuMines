from django.urls import path
from service_analytics.views import (
    ServiceAnalyticsOverviewAPIView,
    RecentActivityAPIView,
    CreateServiceExecutionView,
    ToolDataInsightsAPIView,
    ReportsListView,
)

from .views_reports import ServiceReportsView

urlpatterns = [
    path('reports/', ServiceReportsView.as_view(), name='service-analytics-reports'),
    path('overview/', ServiceAnalyticsOverviewAPIView.as_view(), name='service-analytics-overview'),
    path('recent-activity/', RecentActivityAPIView.as_view(), name='service-analytics-recent-activity'),
    path('executions/create/', CreateServiceExecutionView.as_view(), name='service-execution-create'),
    path('tool-data/', ToolDataInsightsAPIView.as_view(), name='service-analytics-tool-data'),
    path('reports/', ReportsListView.as_view(), name='service-analytics-reports'),
]
