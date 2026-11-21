"""Ethical walls: screened people cannot reach a walled matter's documents, folders or the matter itself
through any route, whatever their role, membership or shares; walls never cross organisations."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.access import (accessible_files, can_access_file, can_access_folder, get_file_for_user,
                         matters_for_user)
from core.models import EthicalWall, File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess, FileFolderLink
from document_operations.utils import get_or_create_folder_tree


class EthicalWallTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Wall firm")
        cls.other = Client.objects.create(name="Other firm")
        cls.admin = make_user("admin@wall.test", cls.firm, groups=["Admin"])
        cls.compliance = make_user("compliance@wall.test", cls.firm, groups=["ComplianceOfficer"])
        cls.lawyer = make_user("lawyer@wall.test", cls.firm, groups=["Client"])
        cls.conflicted = make_user("conflicted@wall.test", cls.firm, groups=["Client"])
        cls.conflicted_admin = make_user("conflicted-admin@wall.test", cls.firm, groups=["Admin"])
        cls.outsider = make_user("outsider@other.test", cls.other, groups=["Admin"])
        cls.matter = Matter.objects.create(client=cls.firm, name="Project Falcon", created_by=cls.lawyer)
        cls.matter.members.add(cls.lawyer, cls.conflicted)   # membership never overrides a wall
        cls.open_matter = Matter.objects.create(client=cls.firm, name="Open matter", created_by=cls.lawyer)
        cls.open_matter.members.add(cls.lawyer, cls.conflicted)

    def setUp(self):
        cache.clear()
        self.folder = get_or_create_folder_tree(["Deal docs"], user=self.lawyer, project_id=self.matter.name, service_id="matter")
        self.secret = self.doc("spa.pdf", self.matter.name, self.folder)
        open_folder = get_or_create_folder_tree(["Notes"], user=self.lawyer, project_id=self.open_matter.name, service_id="matter")
        self.public = self.doc("notes.pdf", self.open_matter.name, open_folder)
        # An explicit share never overrides a wall either.
        EffectiveAccess.objects.create(user=self.conflicted, file=self.secret, can_download=True)

    def doc(self, name, project, folder):
        f = File.objects.create(user=self.lawyer, run=Run.objects.create(user=self.lawyer), filename=name,
                                filepath=f"/nonexistent/{name}", project_id=project, service_id="matter", md5_hash=name)
        FileFolderLink.objects.create(file=f, folder=folder)
        return f

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def wall(self, *people, actor=None):
        r = self.api(actor or self.compliance).post("/api/v1/core/ethical-walls/", {
            "matter_id": str(self.matter.id), "reason": "Acts for the counterparty", "user_ids": [p.pk for p in people]},
            format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    @staticmethod
    def fresh(user):
        return type(user).objects.get(pk=user.pk)   # walls are computed once per user object

    def test_before_a_wall_the_member_sees_the_matter(self):
        self.assertTrue(can_access_file(self.conflicted, self.secret))

    @mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True)
    def test_screened_people_lose_every_route_to_the_matter(self, _scope):
        self.wall(self.conflicted, self.conflicted_admin)
        for person in (self.conflicted, self.conflicted_admin):
            user = self.fresh(person)
            self.assertFalse(can_access_file(user, self.secret))
            self.assertFalse(can_access_folder(user, self.folder))
            self.assertNotIn(self.secret.pk, accessible_files(user).values_list("pk", flat=True))
            self.assertIn(self.public.pk, accessible_files(user).values_list("pk", flat=True))   # other matters unaffected
            self.assertFalse(matters_for_user(user).filter(pk=self.matter.pk).exists())
            api = self.api(user)
            names = [d["filename"] for d in api.get("/api/v1/documents/table/?page_size=100").json()["results"]]
            self.assertNotIn("spa.pdf", names)
            self.assertEqual(api.get(f"/api/v1/core/download/?file_id={self.secret.pk}").status_code, 404)
            self.assertEqual(api.get(f"/api/v1/documents/table/?folder={self.folder.pk}").status_code, 404)
            self.assertEqual(api.get(f"/api/v1/core/projects/{self.matter.pk}/").status_code, 404)
        # The rest of the team is unaffected.
        self.assertTrue(can_access_file(self.fresh(self.lawyer), self.secret))
        self.assertTrue(can_access_file(self.fresh(self.admin), self.secret))

    def test_wall_covers_documents_added_later(self):
        self.wall(self.conflicted)
        later = self.doc("side-letter.pdf", self.matter.name, self.folder)
        self.assertFalse(can_access_file(self.fresh(self.conflicted), later))
        from django.http import Http404
        with self.assertRaises(Http404):
            get_file_for_user(self.fresh(self.conflicted), later.pk)

    def test_lifting_restores_access_and_keeps_the_record(self):
        wall_id = self.wall(self.conflicted)
        r = self.api(self.compliance).post(f"/api/v1/core/ethical-walls/{wall_id}/lift/", {"reason": "Conflict cleared"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(can_access_file(self.fresh(self.conflicted), self.secret))
        self.assertTrue(EthicalWall.objects.filter(pk=wall_id).exists())

    def test_who_may_manage_walls(self):
        body = {"matter_id": str(self.matter.id), "user_ids": [self.conflicted.pk]}
        self.assertEqual(self.api(self.lawyer).post("/api/v1/core/ethical-walls/", body, format="json").status_code, 403)
        # Nobody screens themselves.
        r = self.api(self.compliance).post("/api/v1/core/ethical-walls/", {**body, "user_ids": [self.compliance.pk]}, format="json")
        self.assertEqual(r.status_code, 400)
        wall_id = self.wall(self.conflicted_admin)
        # A screened administrator can neither see nor change the wall around them.
        screened = self.api(self.fresh(self.conflicted_admin))
        self.assertEqual(screened.get("/api/v1/core/ethical-walls/").json()["results"], [])
        self.assertEqual(screened.post(f"/api/v1/core/ethical-walls/{wall_id}/lift/", {"reason": "x"}, format="json").status_code, 404)
        self.assertEqual(screened.put(f"/api/v1/core/ethical-walls/{wall_id}/people/", {"user_ids": [self.lawyer.pk]},
                                      format="json").status_code, 404)

    def test_walls_never_cross_organisations(self):
        wall_id = self.wall(self.conflicted)
        outsider = self.api(self.outsider)
        self.assertEqual(outsider.get("/api/v1/core/ethical-walls/").json()["results"], [])
        self.assertEqual(outsider.post(f"/api/v1/core/ethical-walls/{wall_id}/lift/", {"reason": "x"}, format="json").status_code, 404)
        # Cannot wall another organisation's matter or screen its people.
        r = outsider.post("/api/v1/core/ethical-walls/", {"matter_id": str(self.matter.id), "user_ids": [self.lawyer.pk]}, format="json")
        self.assertEqual(r.status_code, 404)
        other_matter = Matter.objects.create(client=self.other, name="Theirs", created_by=self.outsider)
        r = outsider.post("/api/v1/core/ethical-walls/", {"matter_id": str(other_matter.id), "user_ids": [self.lawyer.pk]}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_change_the_screened_people(self):
        wall_id = self.wall(self.conflicted)
        r = self.api(self.compliance).put(f"/api/v1/core/ethical-walls/{wall_id}/people/",
                                          {"user_ids": [self.conflicted_admin.pk]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(can_access_file(self.fresh(self.conflicted), self.secret))
        self.assertFalse(can_access_file(self.fresh(self.conflicted_admin), self.secret))

    def test_governance_options_are_scoped_and_hide_walled_matters(self):
        self.assertEqual(self.api(self.lawyer).get("/api/v1/core/governance/options/").status_code, 403)
        d = self.api(self.compliance).get("/api/v1/core/governance/options/?people=wall.test").json()
        self.assertEqual(sorted(m["name"] for m in d["matters"]), ["Open matter", "Project Falcon"])
        emails = {p["email"] for p in d["people"]}
        self.assertIn("conflicted@wall.test", emails)
        self.assertNotIn("compliance@wall.test", emails)          # never yourself
        self.assertNotIn("outsider@other.test", emails)           # never another organisation
        self.wall(self.conflicted_admin)
        d = self.api(self.fresh(self.conflicted_admin)).get("/api/v1/core/governance/options/").json()
        self.assertEqual([m["name"] for m in d["matters"]], ["Open matter"])
