import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.db import models


def fix_b64_padding(b64_string):
    """Ensure proper base64 padding for decoding."""
    return b64_string + '=' * (-len(b64_string) % 4)


def _keys():
    """ENCRYPTION_SECRET_KEY: one key, or a comma-separated list during rotation (the first encrypts; any decrypts)."""
    raw = os.getenv("ENCRYPTION_SECRET_KEY")
    if not raw:
        raise ValueError("Missing ENCRYPTION_SECRET_KEY in environment")
    keys = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        try:
            keys.append(AESGCM(base64.urlsafe_b64decode(fix_b64_padding(part))))
        except Exception as e:
            raise ValueError(f"Invalid ENCRYPTION_SECRET_KEY: {str(e)}")
    return keys


class EncryptedCharField(models.CharField):
    """
    Custom field that transparently encrypts/decrypts CharField data using AES-GCM.
    Key rotation: set ENCRYPTION_SECRET_KEY="<new>,<old>", run `manage.py rotate_encryption_keys`, then remove <old>.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ciphers = _keys()
        self._aesgcm = self._ciphers[0]

    def get_prep_value(self, value):
        if value is None:
            return value
        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, value.encode(), None)
        return base64.b64encode(nonce + ciphertext).decode()

    def decrypt_strict(self, value):
        """Plaintext, trying every configured key; raises ValueError when no key opens it."""
        raw = base64.b64decode(value)
        for cipher in self._ciphers:
            try:
                return cipher.decrypt(raw[:12], raw[12:], None).decode()
            except Exception:
                continue
        raise ValueError("No configured key decrypts this value.")

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        try:
            return self.decrypt_strict(value)
        except Exception:
            return value  # fallback for corrupted/malformed data

    def to_python(self, value):
        if value is None or isinstance(value, str):
            try:
                return self.decrypt_strict(value)
            except Exception:
                return value  # assume already decrypted or plaintext
        return value
