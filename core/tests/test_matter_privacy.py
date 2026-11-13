"""
Matter access must not leak documents between colleagues whose folders share a name.

Two people in one organisation each keep a "Project Atlas" folder. Being on the team of
a matter called "Project Atlas" gives access to the files of that matter's TEAM, never to
everyone's files that happen to use the same project name.
"""
from django.test import TestCase

from core.access import accessible_files, can_access_file
from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


def doc(owner, name, project="Project Atlas"):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(filename=name, filepath=f"/x/{owner.id}/{name}", user=owner, run=run,
                               project_id=project, service_id="S", md5_hash=f"{owner.id}-{name}")


class MatterPrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.alice = make_user("alice@a.test", cls.firm, groups=["Client"])
        cls.bob = make_user("bob@a.test", cls.firm, groups=["Client"])
        cls.alice_doc = doc(cls.alice, "alice-notes.pdf")
        cls.bob_doc = doc(cls.bob, "bob-private.pdf")           # same project name, Bob's own folder
        cls.matter = Matter.objects.create(client=cls.firm, name="Project Atlas", created_by=cls.alice)
        cls.matter.members.add(cls.alice)

    def test_same_project_name_does_not_expose_a_colleagues_files(self):
        self.assertFalse(can_access_file(self.alice, self.bob_doc))
        self.assertNotIn(self.bob_doc.id, set(accessible_files(self.alice).values_list("id", flat=True)))
        self.assertFalse(can_access_file(self.bob, self.alice_doc))

    def test_team_members_share_the_matters_files(self):
        self.matter.members.add(self.bob)
        self.assertTrue(can_access_file(self.alice, self.bob_doc))
        self.assertTrue(can_access_file(self.bob, self.alice_doc))

    def test_removed_member_loses_access(self):
        self.matter.members.add(self.bob)
        self.matter.members.remove(self.bob)
        self.assertFalse(can_access_file(self.bob, self.alice_doc))
        self.assertFalse(can_access_file(self.alice, self.bob_doc))
