from types import SimpleNamespace

from django.core.cache import cache
from django.http import Http404
from django.test import SimpleTestCase, override_settings

from core.task_access import register_task, require_task_owner


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class TaskAccessBindingTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.user = SimpleNamespace(pk=1, client_id=10, is_active=True)
        self.task = SimpleNamespace(id="synthetic-task")
        register_task(self.user, self.task, "risk")

    def test_only_requester_in_original_tenant_can_read_result(self):
        require_task_owner(self.user, self.task.id, "risk")
        for actor in [SimpleNamespace(pk=2, client_id=10, is_active=True),
                      SimpleNamespace(pk=1, client_id=20, is_active=True),
                      SimpleNamespace(pk=1, client_id=10, is_active=False)]:
            with self.subTest(actor=actor), self.assertRaises(Http404):
                require_task_owner(actor, self.task.id, "risk")

    def test_wrong_service_unknown_or_legacy_binding_fails_closed(self):
        for task_id, kind in [(self.task.id, "translation"), ("unknown", "risk")]:
            with self.assertRaises(Http404):
                require_task_owner(self.user, task_id, kind)
        cache.set("owned_task:risk:legacy", self.user.pk)
        with self.assertRaises(Http404):
            require_task_owner(self.user, "legacy", "risk")
