"""Encryption key rotation: with "<new>,<old>" configured, rotate_encryption_keys re-encrypts every encrypted column
under the new key, values still read back, and anything no key can open is reported and left untouched."""
import base64
import io
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.management import call_command
from django.db import connection
from django.test import TestCase

from custom_authentication.models import Client, OrganisationSSO


def raw(pk):
    with connection.cursor() as cur:
        cur.execute(f'SELECT client_secret FROM "{OrganisationSSO._meta.db_table}" WHERE id = %s', [pk])
        return cur.fetchone()[0]


def opens(cipher, stored):
    data = base64.b64decode(stored)
    try:
        cipher.decrypt(data[:12], data[12:], None)
        return True
    except Exception:
        return False


class KeyRotationTests(TestCase):
    def setUp(self):
        self.field = OrganisationSSO._meta.get_field("client_secret")
        self.saved = (self.field._ciphers, self.field._aesgcm)
        self.addCleanup(self._restore)
        self.old = self.field._ciphers[0]
        self.new = AESGCM(os.urandom(32))

    def _restore(self):
        self.field._ciphers, self.field._aesgcm = self.saved

    def use(self, *ciphers):
        self.field._ciphers, self.field._aesgcm = list(ciphers), ciphers[0]

    def test_rotation_moves_values_to_the_new_key(self):
        cfg = OrganisationSSO.objects.create(client=Client.objects.create(name="Rotate"), client_secret="s3cret-value")
        self.assertTrue(opens(self.old, raw(cfg.pk)))
        self.use(self.new, self.old)                                  # "<new>,<old>"
        self.assertEqual(OrganisationSSO.objects.get(pk=cfg.pk).client_secret, "s3cret-value")
        out = io.StringIO()
        call_command("rotate_encryption_keys", "--dry-run", stdout=out)
        self.assertTrue(opens(self.old, raw(cfg.pk)))                 # dry run changes nothing
        call_command("rotate_encryption_keys", stdout=out)
        self.assertTrue(opens(self.new, raw(cfg.pk)))
        self.assertFalse(opens(self.old, raw(cfg.pk)))
        self.use(self.new)                                            # old key removed
        self.assertEqual(OrganisationSSO.objects.get(pk=cfg.pk).client_secret, "s3cret-value")
        self.assertIn("0 unreadable", out.getvalue())

    def test_values_no_key_opens_are_reported_and_untouched(self):
        cfg = OrganisationSSO.objects.create(client=Client.objects.create(name="Broken"), client_secret="x")
        garbage = base64.b64encode(os.urandom(40)).decode()
        with connection.cursor() as cur:                              # corrupt the stored value directly
            cur.execute(f'UPDATE "{OrganisationSSO._meta.db_table}" SET client_secret = %s WHERE id = %s', [garbage, cfg.pk])
        self.use(self.new, self.old)
        out = io.StringIO()
        call_command("rotate_encryption_keys", stdout=out)
        self.assertEqual(raw(cfg.pk), garbage)
        self.assertIn("1 unreadable", out.getvalue())
