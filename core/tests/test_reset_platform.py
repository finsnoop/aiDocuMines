"""reset_platform: one account and its organisation remain with reference data; everything else goes.
Dry run by default, exact confirmation, refuses while documents remain."""
import io
from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase
from django.utils import timezone

from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import AdminAuditEvent, Client, CustomUser, OrganisationPolicy, UserActivityLog


class ResetPlatformTests(TransactionTestCase):
    # TransactionTestCase: the command commits its own transaction (deferred foreign keys are checked at commit).

    def setUp(self):
        from contracts.models import Benchmark, Contract, Playbook
        from document_translation.models import TranslationLanguage
        from oauth2_provider.models import AccessToken, Application
        from system_settings.models import VerticalSetting
        self.home = Client.objects.create(name="Datasqan")
        self.other = Client.objects.create(name="Old client")
        self.me = make_user("me@datasqan.test", self.home, superuser=True, groups=["Admin"])
        self.colleague = make_user("colleague@datasqan.test", self.home, groups=["Client"])
        self.stranger = make_user("someone@oldclient.test", self.other, groups=["Admin"])
        OrganisationPolicy.objects.create(client=self.home, watermark_downloads=True)
        OrganisationPolicy.objects.create(client=self.other)
        VerticalSetting.objects.create(tenant=None, key="finance", enabled=False)
        VerticalSetting.objects.create(tenant=self.home, key="immigration", enabled=False)
        VerticalSetting.objects.create(tenant=self.other, key="knowledge", enabled=False)
        self.matter = Matter.objects.create(client=self.home, name="Old matter", created_by=self.colleague)
        self.matter.members.add(self.me, self.colleague)
        Contract.objects.create(client=self.home, title="Old contract", owner=self.me)
        Playbook.objects.create(client=self.other, name="Old playbook")
        Benchmark.objects.create(client=self.home, name="Old benchmark")
        UserActivityLog.objects.create(user=self.me, event="LOGIN")
        AdminAuditEvent.objects.create(actor=self.me, client=self.home, action="test.event")
        self.spa = Application.objects.create(name="spa", client_type="public", authorization_grant_type="authorization-code")
        theirs = Application.objects.create(name="theirs", user=self.stranger, client_type="confidential",
                                            authorization_grant_type="password")
        AccessToken.objects.create(user=self.me, token="tok-me", application=self.spa,
                                   expires=timezone.now() + timedelta(hours=1), scope="read")
        AccessToken.objects.create(user=self.stranger, token="tok-them", application=theirs,
                                   expires=timezone.now() + timedelta(hours=1), scope="read")
        self.languages = TranslationLanguage.objects.count()
        if not self.languages:
            TranslationLanguage.objects.create(**{f.name: ("en" if f.name in ("code", "language_code") else "English")
                                                  for f in TranslationLanguage._meta.concrete_fields
                                                  if not f.primary_key and not f.null and not f.has_default()})
            self.languages = 1

    def run_cmd(self, *args):
        out = io.StringIO()
        call_command("reset_platform", "--keep", "me@datasqan.test", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing_and_execute_needs_exact_confirmation(self):
        text = self.run_cmd()
        self.assertIn("Dry run", text)
        self.assertIn("Other people removed: 2", text)
        self.assertEqual(CustomUser.objects.count(), 3)
        with self.assertRaises(CommandError):
            self.run_cmd("--execute", "--confirm", "RESET someone-else")
        self.assertEqual(CustomUser.objects.count(), 3)

    def test_refuses_while_documents_remain(self):
        File.objects.create(user=self.me, run=Run.objects.create(user=self.me), filename="x.pdf", filepath="/x.pdf",
                            project_id="P", service_id="matter", md5_hash="x")
        with self.assertRaises(CommandError) as ctx:
            self.run_cmd("--execute", "--confirm", "RESET me@datasqan.test")
        self.assertIn("purge_documents", str(ctx.exception))
        self.assertEqual(CustomUser.objects.count(), 3)

    def test_execute_keeps_one_account_its_organisation_and_reference_data(self):
        from contracts.models import Benchmark, Contract, Playbook
        from document_translation.models import TranslationLanguage
        from oauth2_provider.models import AccessToken, Application
        from system_settings.models import VerticalSetting
        text = self.run_cmd("--execute", "--confirm", "RESET me@datasqan.test")
        self.assertIn("Reset complete", text)
        self.assertEqual(list(CustomUser.objects.values_list("email", flat=True)), ["me@datasqan.test"])
        me = CustomUser.objects.get()
        self.assertTrue(me.is_superuser and me.is_active)
        self.assertEqual(list(me.groups.values_list("name", flat=True)), ["Admin"])          # role kept
        self.assertEqual(list(Client.objects.values_list("name", flat=True)), ["Datasqan"])
        self.assertTrue(OrganisationPolicy.objects.get(client=self.home).watermark_downloads)
        self.assertEqual(sorted(VerticalSetting.objects.values_list("key", flat=True)), ["finance", "immigration"])
        for model in (Matter, Contract, Playbook, Benchmark, UserActivityLog, AdminAuditEvent, AccessToken):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        names = set(Application.objects.values_list("name", flat=True))
        self.assertIn("spa", names)                                     # the sign-in app (no owner) is kept
        self.assertNotIn("theirs", names)                               # other people's applications go
        self.assertFalse(Application.objects.exclude(user__isnull=True).exclude(user=me).exists())
        self.assertEqual(TranslationLanguage.objects.count(), self.languages)
        self.assertTrue(Group.objects.filter(name="Admin").exists())
