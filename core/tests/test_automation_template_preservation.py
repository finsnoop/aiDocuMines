"""Removing documents keeps template definitions and their authenticated owner."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_file, make_user
from custom_authentication.models import Client
from document_automation.models import AutomationTemplate, AutomationRun, TemplateField


class TemplatePreservationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        org = Client.objects.create(name="Template firm")
        foreign_org = Client.objects.create(name="Foreign template firm")
        cls.owner = make_user("template-owner@example.test", org)
        cls.foreign = make_user("foreign-template@example.test", foreign_org)

    def setUp(self):
        self.source = make_file(self.owner, name="template.txt")
        self.template = AutomationTemplate.objects.create(name="Retained workflow", file=self.source,
            owner=self.owner, template_type="TXT", project_id="matter", service_id="automation",
            client_name=self.owner.username)
        TemplateField.objects.create(template=self.template, name="client_name", required=True,
                                     default_value="Retained value")
        patcher = mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def remove_source(self):
        self.source.delete()
        self.template.refresh_from_db()

    def test_document_delete_preserves_template_fields_and_owner_scoped_visibility(self):
        self.remove_source()
        self.assertIsNone(self.template.file_id)
        self.assertEqual(self.template.owner_id, self.owner.pk)
        self.assertEqual(self.template.fields.get().default_value, "Retained value")
        url = f"/api/v1/automation/templates/{self.template.pk}/"
        self.assertEqual(self.client_for(self.owner).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.foreign).get(url).status_code, 404)
        fields_url = f"/api/v1/automation/template-fields/?template_id={self.template.pk}"
        self.assertEqual(self.client_for(self.owner).get(fields_url).json()[0]["name"], "client_name")
        self.assertEqual(self.client_for(self.foreign).get(fields_url).status_code, 404)

    @mock.patch("document_automation.views.process_automation_task.delay")
    def test_submit_missing_source_returns_actionable_conflict_without_dispatch(self, queue):
        self.remove_source()
        for endpoint, data in (("submit-automation", {"input_data": {}}),
                               ("bulk-submit-automation", {"bulk_data": [{}]})):
            url = f"/api/v1/automation/{endpoint}/?template_id={self.template.pk}"
            response = self.client_for(self.owner).post(url, data, format="json")
            self.assertEqual(response.status_code, 409, response.content)
            self.assertIn("no source document", response.json()["error"])
            self.assertEqual(self.client_for(self.foreign).post(url, data, format="json").status_code, 404)
        queue.assert_not_called()
        self.assertFalse(AutomationRun.objects.exists())

    @mock.patch("document_automation.tasks.process_template")
    def test_queued_job_with_removed_template_source_fails_cleanly_and_keeps_definition(self, render):
        from document_automation.tasks import process_automation_task
        run = AutomationRun.objects.create(template=self.template, project_id="matter", service_id="automation",
                                           client_name=self.owner.username)
        self.remove_source()
        result = process_automation_task.run(str(run.pk))
        self.assertIn("no source document", result["error"])
        run.refresh_from_db()
        self.assertEqual(run.status, "Failed")
        self.assertEqual(run.results.get().status, "Failed")
        self.assertTrue(AutomationTemplate.objects.filter(pk=self.template.pk).exists())
        render.assert_not_called()

    def test_create_template_requires_accessible_source_and_assigns_authenticated_owner(self):
        url = "/api/v1/automation/templates/"
        data = {"name": "New definition", "file": self.source.pk, "template_type": "TXT",
                "project_id": "matter", "service_id": "automation", "client_name": self.foreign.username}
        self.assertEqual(self.client_for(self.foreign).post(url, data, format="json").status_code, 404)
        response = self.client_for(self.owner).post(url, data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        created = AutomationTemplate.objects.get(pk=response.json()["id"])
        self.assertEqual(created.owner_id, self.owner.pk)
        self.assertEqual(created.client_name, self.owner.username)
        data["file"] = None
        self.assertEqual(self.client_for(self.owner).post(url, data, format="json").status_code, 400)
