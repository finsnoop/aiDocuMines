"""Reset the platform to one account: everything else (other people, other organisations, all business data and AI
usage) is removed. For a clean demonstration environment. Run purge_documents first: documents, their files on disk
and the search indexes are removed by that tested command, and this one refuses to run while documents remain.

    python manage.py reset_platform --keep you@firm.com                                   # dry run: what would go
    python manage.py reset_platform --keep you@firm.com --execute --confirm "RESET <keep email>"

Kept:
  * the kept person (made an active platform superuser) and their organisation, with its policy, single sign-on
    and system settings, and its feature switches (plus the platform-wide switches)
  * reference data: roles and permissions, content types, scheduled jobs, the translation language list,
    AI model prices, platform-wide settings
  * OAuth applications with no owner or owned by the kept person (the browser sign-in app)
Removed: every other row of the platform's own apps (matters, contracts, playbooks, benchmarks, workflows,
templates, clauses, integrations, vertical records, AI usage and cost records, evaluations, assistant history,
notifications, activity and the administrative audit trail), all sessions, tokens and sign-in codes (the kept person
signs in again), and all other people and organisations. Afterwards the Django cache is cleared.
There is no undo: take a database backup first.
"""
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

KEEP = {
    "auth.Group", "auth.Permission", "auth.Group_permissions", "contenttypes.ContentType",
    "document_translation.TranslationLanguage", "home_dash_analytics.ModelPrice",
    "system_settings.PlatformGlobalSettings", "file_elasticsearch.DummyModel",
}
KEEP_APPS = {"django_celery_beat", "sessions_keep_none"}           # scheduled jobs are configuration
# Kept tables whose rows are filtered to the kept person / organisation after the wipe (by ORM deletes).
SCOPED = {
    "custom_authentication.CustomUser", "custom_authentication.Client", "custom_authentication.OrganisationPolicy",
    "custom_authentication.OrganisationSSO", "system_settings.SystemSettings", "system_settings.VerticalSetting",
    "oauth2_provider.Application",
}
# Framework tables that hold sign-in state or logs: wiped.
FRAMEWORK_WIPE_APPS = {"sessions", "authtoken", "admin", "oauth2_provider"}


def label(m):
    return f"{m._meta.app_label}.{m._meta.object_name}"


def project_app_labels():
    base = str(settings.BASE_DIR)
    return {a.label for a in apps.get_app_configs() if str(a.path).startswith(base)}


def plan():
    """(wipe models, kept models) over every model including auto-created many-to-many tables."""
    project = project_app_labels()
    wipe, keep = [], []
    for m in apps.get_models(include_auto_created=True):
        lbl, app = label(m), m._meta.app_label
        if not m._meta.managed or m._meta.proxy:
            continue
        if lbl in KEEP or lbl in SCOPED or app in KEEP_APPS:
            keep.append(m)
        elif m._meta.auto_created and (label(m._meta.auto_created) in SCOPED or label(m._meta.auto_created) in KEEP):
            keep.append(m)          # e.g. the kept person's role memberships: filtered by the user delete
        elif app in project or app in FRAMEWORK_WIPE_APPS:
            wipe.append(m)
        else:
            keep.append(m)
    return wipe, keep


def blocking_references(wipe, keep):
    """Columns of kept tables pointing at wiped tables: (model, field). Nullable ones are cleared first;
    non-nullable ones would make the wipe fail and are reported."""
    wiped = {m._meta.db_table for m in wipe}
    out = []
    for m in keep:
        for f in m._meta.concrete_fields:
            if f.is_relation and f.related_model is not None and f.related_model._meta.db_table in wiped:
                out.append((m, f))
    return out


class Command(BaseCommand):
    help = "Reset the platform to one account (dry run unless --execute and --confirm)."

    def add_arguments(self, parser):
        parser.add_argument("--keep", required=True, help="Email of the one account to keep (made a platform superuser).")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--confirm", default="", help='Must be exactly "RESET <keep email>" with --execute.')

    def handle(self, *args, keep, execute, confirm, **opts):
        from core.models import File
        from custom_authentication.models import Client, CustomUser
        keeper = CustomUser.objects.filter(email__iexact=keep).select_related("client").first()
        if keeper is None:
            raise CommandError(f"No account {keep!r}. Nothing was changed.")
        if keeper.client_id is None:
            raise CommandError("The kept account has no organisation; give it one first. Nothing was changed.")
        wipe, kept = plan()
        refs = blocking_references(wipe, kept)
        hard = [(m, f) for m, f in refs if not f.null]
        self.stdout.write(f"Keep: {keeper.email} (organisation {keeper.client.name!r}, id {keeper.client_id})")
        with connection.cursor() as cur:
            counts = []
            for m in wipe:
                cur.execute(f'SELECT COUNT(*) FROM "{m._meta.db_table}"')
                counts.append((cur.fetchone()[0], m._meta.db_table))
        total = sum(n for n, _ in counts)
        self.stdout.write(f"Tables wiped: {len(wipe)}, rows: {total}")
        for n, table in sorted(counts, reverse=True):
            if n:
                self.stdout.write(f"  {table}: {n}")
        self.stdout.write(f"Other people removed: {CustomUser.objects.exclude(pk=keeper.pk).count()}; "
                          f"other organisations removed: {Client.objects.exclude(pk=keeper.client_id).count()}")
        for m, f in refs:
            self.stdout.write(f"  kept {m._meta.db_table}.{f.column} -> wiped table: {'cleared first' if f.null else 'BLOCKS'}")
        if hard:
            raise CommandError("Kept tables have required links to wiped tables (listed above); nothing was changed.")
        remaining = File.objects.count()
        if remaining:
            raise CommandError(f"{remaining} documents remain. Run purge_documents --execute --all-tenants first "
                               "(it removes their files and search entries). Nothing was changed.")
        if not execute:
            self.stdout.write(self.style.WARNING("Dry run: nothing was changed. Add --execute --confirm "
                                                 f'"RESET {keeper.email}" to reset.'))
            return
        if confirm != f"RESET {keeper.email}":
            raise CommandError(f'Type --confirm "RESET {keeper.email}" exactly. Nothing was changed.')

        with transaction.atomic():
            for m, f in refs:
                m._base_manager.exclude(**{f"{f.name}__isnull": True}).update(**{f.name: None})
            # DELETE, not TRUNCATE: PostgreSQL refuses TRUNCATE on a table a kept table references, and Django's
            # foreign keys are DEFERRABLE INITIALLY DEFERRED, so any order is valid inside this transaction.
            with connection.cursor() as cur:
                for m in wipe:
                    cur.execute(f'DELETE FROM "{m._meta.db_table}"')
            CustomUser.objects.exclude(pk=keeper.pk).delete()
            Client.objects.exclude(pk=keeper.client_id).delete()
            from oauth2_provider.models import Application
            Application.objects.exclude(user__isnull=True).exclude(user=keeper).delete()
            from system_settings.models import VerticalSetting
            VerticalSetting.objects.exclude(tenant__isnull=True).exclude(tenant_id=keeper.client_id).delete()
            keeper.is_active = keeper.is_superuser = keeper.is_staff = True
            keeper.save(update_fields=["is_active", "is_superuser", "is_staff"])
        try:
            from django.core.cache import cache
            cache.clear()
        except Exception as exc:          # the reset itself is complete; say so
            self.stdout.write(self.style.WARNING(f"Cache could not be cleared ({exc}); clear it or restart Redis."))
        self.stdout.write(self.style.SUCCESS(
            f"Reset complete: {total} rows removed; {keeper.email} is the only account. Sign in again."))
