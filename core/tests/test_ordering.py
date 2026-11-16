"""Paginated tables are sorted by the server, across all pages, using only columns each view allows."""
from django.test import RequestFactory, TestCase
from rest_framework.request import Request
from rest_framework.test import APIClient

from core.models import Matter
from core.ordering import ordering_from
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


def _req(qs):
    return Request(RequestFactory().get("/x/" + qs))


class OrderingHelperTests(TestCase):
    ALLOWED = {"name": "name", "activity": "updated_at"}

    def test_known_key_ascending_and_descending(self):
        self.assertEqual(ordering_from(_req("?ordering=name"), self.ALLOWED, "-updated_at")[0], "name")
        self.assertEqual(ordering_from(_req("?ordering=-name"), self.ALLOWED, "-updated_at")[0], "-name")

    def test_unknown_or_injected_keys_fall_back_to_default(self):
        for raw in ("", "password", "-client__secret", "name;drop", "user__password"):
            self.assertEqual(ordering_from(_req(f"?ordering={raw}"), self.ALLOWED, "-updated_at")[0], "-updated_at")


class MatterListOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("o1@firm-a.test", cls.firm, groups=["Client"])
        for name in ("Bravo", "alpha", "Charlie"):
            m = Matter.objects.create(client=cls.firm, name=name, created_by=cls.user)
            m.members.add(cls.user)

    def _names(self, ordering):
        c = APIClient()
        c.force_authenticate(user=self.user)
        r = c.get("/api/v1/core/projects/", {"ordering": ordering, "page_size": 2})
        self.assertEqual(r.status_code, 200)
        return [m["name"] for m in r.json()["results"]]

    def test_sorted_across_pages_not_just_the_page(self):
        self.assertEqual(self._names("name"), ["alpha", "Bravo"] if self._ci() else ["Bravo", "Charlie"])
        self.assertEqual(self._names("-name")[0], "alpha" if not self._ci() else "Charlie")

    def _ci(self):
        # PostgreSQL collation decides whether 'alpha' sorts before 'Bravo'; both are valid server orders.
        return list(Matter.objects.order_by("name").values_list("name", flat=True))[0] == "alpha"
