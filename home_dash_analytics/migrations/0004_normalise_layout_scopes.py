"""
Dashboard layouts, step 1 of 2: one row per (user, matter, service).

0002 created the table with nullable project_id/service_id and unique_together.
In PostgreSQL NULLs never collide, so a user could collect several "global"
layouts (project/service NULL) and readers picked one at random. This keeps the
newest row of each scope, stores "no scope" as an empty string and replaces
unique_together with a real constraint.
"""
from django.db import migrations, models


def normalise_scopes(apps, schema_editor):
    DashboardLayout = apps.get_model("home_dash_analytics", "DashboardLayout")
    seen = set()
    stale = []
    for row in DashboardLayout.objects.order_by("-updated_at", "-id").only("id", "user_id", "project_id", "service_id").iterator():
        key = (row.user_id, row.project_id or "", row.service_id or "")
        if key in seen:
            stale.append(row.id)
        else:
            seen.add(key)
    for i in range(0, len(stale), 1000):
        DashboardLayout.objects.filter(id__in=stale[i:i + 1000]).delete()
    DashboardLayout.objects.filter(project_id__isnull=True).update(project_id="")
    DashboardLayout.objects.filter(service_id__isnull=True).update(service_id="")


class Migration(migrations.Migration):

    dependencies = [
        ("home_dash_analytics", "0003_add_ui_prefs"),
    ]

    operations = [
        migrations.AlterUniqueTogether(name="dashboardlayout", unique_together=set()),
        migrations.RunPython(normalise_scopes, migrations.RunPython.noop),
    ]
