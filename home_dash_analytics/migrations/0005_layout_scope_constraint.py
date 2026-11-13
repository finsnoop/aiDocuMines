"""
Dashboard layouts, step 2 of 2: store "no scope" as an empty string and enforce one
row per (user, matter, service). Kept separate from step 1 because PostgreSQL will
not alter a table that has pending trigger events from the clean-up in one transaction.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("home_dash_analytics", "0004_normalise_layout_scopes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dashboardlayout", name="project_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="dashboardlayout", name="service_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="dashboardlayout", name="layout",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddConstraint(
            model_name="dashboardlayout",
            constraint=models.UniqueConstraint(fields=("user", "project_id", "service_id"), name="uniq_dashboard_layout_scope"),
        ),
    ]
