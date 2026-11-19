"""Exercise the configured Django RedisCache with real prefixed Redis keys."""
import hashlib
import uuid

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache
from django.test import SimpleTestCase, override_settings

from core.document_purge import PurgeBlocked, clear_document_cache, document_cache_keys


def irreversible_key(key, prefix, version):
    return hashlib.sha256(f"{prefix}:{version}:{key}".encode()).hexdigest()


class DocumentPurgeRedisTests(SimpleTestCase):
    def setUp(self):
        config = dict(settings.CACHES["default"])
        config["KEY_PREFIX"] = "document-purge-regression-" + uuid.uuid4().hex
        self.configuration = config
        override = override_settings(CACHES={"default": config})
        override.enable()
        self.addCleanup(override.disable)
        self.backend = caches["default"]
        self.assertIsInstance(self.backend, RedisCache, "Run this integration test with the deployed Django RedisCache settings.")
        self.redis = self.backend._cache.get_client(write=True)
        self.raw_keys = set()
        self.addCleanup(self.cleanup_keys)

    def cleanup_keys(self):
        if self.raw_keys:
            self.redis.delete(*self.raw_keys)

    def put(self, key, value, *, version=None):
        self.backend.set(key, value, timeout=120, version=version)
        self.raw_keys.add(self.backend.make_key(key, version=version))

    def test_real_redis_scan_and_deletion_stay_in_document_namespace_prefix_and_version(self):
        self.put("pair-assistant:answer", {"source_excerpt": "Sensitive document answer"})
        self.put("stats:tenant:user", {"pii_count": 4})
        self.put("text-preparation:source:provenance", {"chars": 40})
        self.put("agents:resource-report", {"state": "Keep platform operations"})
        self.put("pair-assistant:answer", {"version": "Keep another cache version"}, version=2)
        external = "another-application:" + uuid.uuid4().hex + ":pair-assistant:answer"
        broker_like = "celery-task-meta-" + uuid.uuid4().hex
        for raw in [external, broker_like]:
            self.redis.set(raw, b"Keep other application/broker data", ex=120)
            self.raw_keys.add(raw)

        keys = document_cache_keys()
        self.assertEqual(keys, {"pair-assistant:answer", "stats:tenant:user", "text-preparation:source:provenance"})
        # Enumeration itself is read-only.
        self.assertIsNotNone(self.backend.get("pair-assistant:answer"))
        clear_document_cache(keys)
        self.assertIsNone(self.backend.get("pair-assistant:answer"))
        self.assertIsNone(self.backend.get("stats:tenant:user"))
        self.assertIsNone(self.backend.get("text-preparation:source:provenance"))
        self.assertEqual(self.backend.get("agents:resource-report"), {"state": "Keep platform operations"})
        self.assertEqual(self.backend.get("pair-assistant:answer", version=2), {"version": "Keep another cache version"})
        self.assertEqual(self.redis.get(external), b"Keep other application/broker data")
        self.assertEqual(self.redis.get(broker_like), b"Keep other application/broker data")

    def test_irreversible_cache_key_configuration_fails_before_enumeration(self):
        config = {**self.configuration, "KEY_FUNCTION": irreversible_key}
        with override_settings(CACHES={"default": config}):
            with self.assertRaisesMessage(PurgeBlocked, "not safely reversible"):
                document_cache_keys()
