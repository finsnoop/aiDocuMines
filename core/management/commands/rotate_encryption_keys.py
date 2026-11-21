"""Re-encrypt every encrypted database column with the current (first) key.

Rotation, for either key (FIELD_ENCRYPTION_KEY: Fernet fields; ENCRYPTION_SECRET_KEY: AES-GCM fields):
  1. Generate a new key.  Fernet: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
                          AES:    python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
  2. Set the variable to "<new>,<old>" (Key Vault secret or .env) and restart the API and workers.
  3. python manage.py rotate_encryption_keys            (add --dry-run first to see the counts)
  4. When it reports 0 unreadable, set the variable to "<new>" only and restart again.

Values that no configured key can open are reported and left untouched (never double-encrypted).
"""
from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import connection, transaction


def encrypted_fields():
    from core.fields.encrypted_char import EncryptedCharField
    try:
        from encrypted_model_fields.fields import EncryptedMixin
    except Exception:          # pragma: no cover
        EncryptedMixin = ()
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        for field in model._meta.concrete_fields:
            if isinstance(field, EncryptedCharField) or (EncryptedMixin and isinstance(field, EncryptedMixin)):
                yield model, field


def _plaintext(field, stored):
    """Decrypt a stored value with any configured key; ValueError when none opens it."""
    from core.fields.encrypted_char import EncryptedCharField
    if isinstance(field, EncryptedCharField):
        return field.decrypt_strict(stored)
    from encrypted_model_fields.fields import CRYPTER
    try:
        return CRYPTER.decrypt(stored.encode()).decode()
    except Exception as exc:
        raise ValueError(str(exc)) from exc


class Command(BaseCommand):
    help = "Re-encrypt all encrypted columns with the current key (see the module docstring for the rotation steps)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count only; change nothing.")

    def handle(self, *args, dry_run=False, **opts):
        total = unreadable = 0
        for model, field in encrypted_fields():
            table, column, pk = model._meta.db_table, field.column, model._meta.pk.column
            with connection.cursor() as cur:
                cur.execute(f'SELECT "{pk}", "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" <> %s', [""])
                rows = cur.fetchall()
            done = bad = 0
            with transaction.atomic():
                for row_pk, stored in rows:
                    try:
                        value = _plaintext(field, stored)
                    except ValueError:
                        bad += 1
                        continue
                    if not dry_run:
                        model._base_manager.filter(pk=row_pk).update(**{field.name: value})
                    done += 1
            if rows:
                self.stdout.write(f"{model._meta.label}.{field.name}: {done} re-encrypted, {bad} unreadable")
            total, unreadable = total + done, unreadable + bad
        verb = "would be re-encrypted" if dry_run else "re-encrypted"
        style = self.style.SUCCESS if not unreadable else self.style.WARNING
        self.stdout.write(style(f"{total} values {verb}; {unreadable} unreadable with the configured keys."))
