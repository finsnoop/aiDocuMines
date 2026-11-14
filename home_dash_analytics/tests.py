from unittest import mock
from django.test import SimpleTestCase
from home_dash_analytics.tasks import compute_home_analytics

class DashboardTaskTests(SimpleTestCase):
    @mock.patch("home_dash_analytics.tasks.HomeAnalyticsSnapshot.objects.update_or_create")
    @mock.patch("home_dash_analytics.tasks.gather_user_dashboard_metrics", return_value={"files": {"total": 2}})
    def test_async_metrics_bind_the_user_and_since_by_name(self, metrics, save):
        save.return_value = (mock.Mock(), True)
        since = "2026-11-01T00:00:00+00:00"
        result = compute_home_analytics.run(42, since)
        metrics.assert_called_once_with(user_id=42, since=since)
        self.assertEqual(save.call_args.kwargs["user_id"], 42)
        self.assertEqual(result, {"files": {"total": 2}})
