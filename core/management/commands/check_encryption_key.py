import os
from django.core.management.base import BaseCommand
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()  # Just in case .env isn’t already loaded

class Command(BaseCommand):
    help = 'Check if FIELD_ENCRYPTION_KEY is valid and usable'

    def handle(self, *args, **kwargs):
        keys = [k.strip() for k in (os.getenv("FIELD_ENCRYPTION_KEY") or "").split(",") if k.strip()]

        if not keys:
            self.stdout.write(self.style.ERROR("FIELD_ENCRYPTION_KEY is missing"))
            return
        if len(keys) > 1:
            self.stdout.write(f"{len(keys)} keys configured (rotation): the first encrypts, all decrypt.")
        key = keys[0]

        try:
            f = Fernet(key)
            test = f.encrypt(b"ping")
            decrypted = f.decrypt(test)
            self.stdout.write(self.style.SUCCESS("FIELD_ENCRYPTION_KEY loaded and works"))
            self.stdout.write(f"Encrypted: {test}")
            self.stdout.write(f"Decrypted: {decrypted}")
        except InvalidToken:
            self.stdout.write(self.style.ERROR("FIELD_ENCRYPTION_KEY is invalid (decryption failed)"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Unexpected error: {str(e)}"))

