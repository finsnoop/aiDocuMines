# integrations/oidc_utils.py

import os
import json
import urllib.parse
import subprocess

from oauth2_provider.models import Application
from django.core.exceptions import ImproperlyConfigured


SUPERUSER_SECRET_PATH = os.getenv(
    "SUPERUSER_SECRET_PATH",
    "/home/aidocumines/Apps/aiDocuMines/logs/.superuser_secrets.json"
)

def load_superuser_secrets():
    try:
        with open(SUPERUSER_SECRET_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load superuser secrets: {e}")

'''
def load_superuser_secrets():
    """
    Securely load superuser secrets from a protected file using sudo.
    """
    try:
        result = subprocess.run(
            ["sudo", "cat", SUPERUSER_SECRET_PATH],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except Exception as e:
        raise ImproperlyConfigured(f"Failed to load superuser secrets: {e}")
'''

def get_or_create_nextcloud_oidc_user(user):
    """
    Ensure the user has a registered OAuth2 Application for Nextcloud OIDC.
    If you're not dynamically registering per user, this can be a no-op.
    """
    if not user.email:
        raise ValueError("User must have an email address.")

    app, _ = Application.objects.get_or_create(
        user=user,
        name="nextcloud_oidc",
        client_type="confidential",  # or "public" if you're using PKCE
        authorization_grant_type="authorization-code",
        defaults={
            "redirect_uris": os.getenv(
                "OIDC_REDIRECT_URI",
                "https://nextcloud.apps.datasqan.com/apps/user_oidc/code"
            ),
        }
    )
    return app


def generate_nextcloud_oidc_url(user) -> str:
    """
    Construct the full Nextcloud OIDC login URL.
    Nextcloud handles state/nonce internally via its stored provider config.
    """
    secrets = load_superuser_secrets()

    client_id = secrets.get("client_id")
    if not client_id:
        raise ImproperlyConfigured("Missing 'client_id' in superuser secrets.")

    nextcloud_base = os.getenv(
        "NEXTCLOUD_URL",
        "https://nextcloud.apps.datasqan.com"
    )

    # Redirect to Nextcloud's OIDC login route (provider ID 1 = "aiDocuMines").
    # Nextcloud will use its stored provider config (client_id, secret, discovery URL)
    # to build the proper authorization request automatically.
    return f"{nextcloud_base}/apps/user_oidc/login/1"

