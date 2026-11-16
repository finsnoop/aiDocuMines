# -*- coding: utf-8 -*-
import os
import base64
from cryptography.fernet import Fernet, InvalidToken

def load_fernet_key():
    key = os.getenv("FIELD_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FIELD_ENCRYPTION_KEY is missing from environment variables.")

    try:
        decoded = base64.urlsafe_b64decode(key)
        print(f"Decoded length: {len(decoded)} bytes")
        if len(decoded) != 32:
            raise ValueError("Decoded key is not 32 bytes.")
        fernet = Fernet(key)
        print("Fernet key is valid and usable.")
        return fernet
    except Exception as e:
        print(f"Failed to load FIELD_ENCRYPTION_KEY: {str(e)}")
        return None

if __name__ == "__main__":
    # Optionally load from .env if running locally
    from dotenv import load_dotenv
    load_dotenv()

    crypter = load_fernet_key()

    # Optional: test encrypt/decrypt roundtrip
    if crypter:
        msg = b"test-message"
        token = crypter.encrypt(msg)
        print(f"Encrypted: {token}")
        decrypted = crypter.decrypt(token)
        print(f"Decrypted: {decrypted}")

