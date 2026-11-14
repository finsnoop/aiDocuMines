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
    # Nextcloud builds the authorisation request from its own stored provider configuration,
    # so no credentials are needed here. (This used to read the platform superuser's secrets
    # file, never used the result, and failed wherever that file was absent.)
    nextcloud_base = os.getenv(
        "NEXTCLOUD_URL",
        "https://nextcloud.apps.datasqan.com"
    )

    # Redirect to Nextcloud's OIDC login route (provider ID 1 = "aiDocuMines").
    # Nextcloud will use its stored provider config (client_id, secret, discovery URL)
    # to build the proper authorization request automatically.
    return f"{nextcloud_base}/apps/user_oidc/login/1"

