"""
Bulk runs belong to the person who started them and to their organisation (a run
saved without its organisation failed every document: "null value in column client_id").
"""
from django.db import transaction
from django.test import TestCase

from core import bulk_registry as br
from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


class BulkRunTenantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.owner = make_user("owner@a.test", cls.firm, groups=["Client"])
        cls.colleague = make_user("colleague@a.test", cls.firm, groups=["Client"])
        run = Run.objects.create(user=cls.owner, status="Completed")
        cls.file = File.objects.create(filename="claim.pdf", filepath="/x/claim.pdf", user=cls.owner, run=run,
                                       project_id="P", service_id="S", md5_hash="m1")

    def _check(self, creator, model_path, key):
        module, name = model_path.rsplit(".", 1)
        model = getattr(__import__(module, fromlist=[name]), name)
        ids = creator(self.file.id, self.colleague.id)
        obj = model.objects.get(pk=ids[key])
        fields = {f.name for f in model._meta.get_fields()}
        if "client" in fields:
            self.assertEqual(obj.client_id, self.firm.id, model_path)
        if "user" in fields:
            self.assertEqual(obj.user_id, self.colleague.id, model_path)   # the person who ran it

    def test_every_creator_records_person_and_organisation(self):
        cases = [
            (br._run_infringement, "ip_litigation.models.InfringementAnalysis", "infringement_analysis_id"),
            (br._run_patent, "ip_litigation.models.PatentDocument", "patent_document_id"),
            (br._run_prior_art, "ip_litigation.models.PriorArtDocument", "prior_art_document_id"),
            (br._run_dsar, "regulatory_compliance.models.DSARRequest", "dsar_request_id"),
            (br._run_redaction, "regulatory_compliance.models.RedactionTask", "redaction_task_id"),
            (br._run_clustering, "document_classification.models.ClusteringRun", None),
            (br._run_structure, "document_structures.models.DocumentStructureRun", None),
        ]
        for creator, model_path, key in cases:
            with self.subTest(model=model_path), transaction.atomic():
                if key is None:
                    ids = creator(self.file.id, self.colleague.id)
                    key = next(iter(ids))
                    module, name = model_path.rsplit(".", 1)
                    model = getattr(__import__(module, fromlist=[name]), name)
                    obj = model.objects.get(pk=ids[key])
                    if "client" in {f.name for f in model._meta.get_fields()}:
                        self.assertEqual(obj.client_id, self.firm.id)
                else:
                    self._check(creator, model_path, key)
