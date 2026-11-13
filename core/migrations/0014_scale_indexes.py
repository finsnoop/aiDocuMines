"""
Indexes for matter document/run lists on large tables.

Built with CREATE INDEX CONCURRENTLY so existing production tables (which may be
very large) stay readable and writable while the index builds.
"""
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False  # required for CONCURRENTLY

    dependencies = [
        ("core", "0013_matter_services"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="file",
            index=models.Index(fields=["project_id", "user"], name="file_project_user"),
        ),
        AddIndexConcurrently(
            model_name="bulkjob",
            index=models.Index(fields=["project_id", "user"], name="bulkjob_project_user"),
        ),
    ]
