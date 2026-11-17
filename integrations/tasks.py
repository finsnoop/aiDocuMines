# integrations/tasks.py

import os
import json
import secrets
import subprocess
from celery import shared_task
from django.contrib.auth import get_user_model

from integrations.oidc_utils import (
    generate_nextcloud_oidc_url,
    get_or_create_nextcloud_oidc_user,
)
from integrations.registry import STATE_REGISTRY, NONCE_REGISTRY
from integrations.models import IntegrationLog


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def provision_nextcloud_user(self, user_id):
    """
    Full provisioning flow for a new user: create OIDC client + sync files.
    Called automatically when a new user is created by an admin.
    """
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        print(f"[provision_nextcloud_user] Starting provisioning for user {user.id}")

        get_or_create_nextcloud_oidc_user(user)

        client_name = user.client.name if hasattr(user, 'client') and user.client else str(user.id)
        sync_user_to_nextcloud_host.delay(user.id, client_name)

        IntegrationLog.objects.create(
            user=user,
            connector="nextcloud",
            status="created",
            details=f"Auto-provisioning initiated for user {user.id}"
        )
        print(f"[provision_nextcloud_user] Provisioning queued for {user.email}")

    except User.DoesNotExist:
        print(f"[provision_nextcloud_user] User {user_id} does not exist")
    except Exception as e:
        try:
            IntegrationLog.objects.create(
                user_id=user_id,
                connector="nextcloud",
                status="error",
                details=f"Auto-provisioning failed: {str(e)}"
            )
        except Exception:
            pass
        print(f"[provision_nextcloud_user] ERROR: {str(e)}")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def generate_nextcloud_url_async(self, user_id):
    """
    Celery task to generate autologin URL for Nextcloud for a given user.
    Includes OIDC URL generation with state/nonce tracking.
    """
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        print(f"[generate_nextcloud_url_async] Starting for user ID {user.id}")

        get_or_create_nextcloud_oidc_user(user)

        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)

        STATE_REGISTRY[user.id] = state
        NONCE_REGISTRY[user.id] = nonce

        url = generate_nextcloud_oidc_url(user)

        IntegrationLog.objects.create(
            user=user,
            connector="nextcloud",
            status="autologin_ready",
            details=f"Autologin URL generated successfully for user {user.id}"
        )
        print(f"[generate_nextcloud_url_async] URL generated: {url}")
        return url

    except User.DoesNotExist:
        IntegrationLog.objects.create(
            user=None,
            connector="nextcloud",
            status="error",
            details=f"User with ID {user_id} does not exist"
        )
        raise

    except Exception as e:
        try:
            IntegrationLog.objects.create(
                user_id=user_id,
                connector="nextcloud",
                status="error",
                details=f"Autologin task failed: {str(e)}"
            )
        except Exception:
            pass
        print(f"[generate_nextcloud_url_async] ERROR: {str(e)}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_user_to_nextcloud_host(self, user_id, client_id):
    """
    Celery task to sync user folder to Nextcloud volume using rsync and OCC scan.
    Must be executed from a worker running on the Docker host.
    """
    try:
        User = get_user_model()
        user = User.objects.get(id=user_id)
        username = f"user_{user.id}"

        print(f"[sync_user_to_nextcloud_host] Starting rsync for user {username}")

        NC_CONTAINER_NAME = os.getenv("NEXTCLOUD_DOCKER_CONTAINER", "srv-captain--nextcloud")
        AIDOCUMINES_DATA = os.getenv("AIDOCUMINES_DATA", "/home/aidocumines/Apps/aiDocuMines/media/uploads")

        user_data_path = os.path.join(AIDOCUMINES_DATA, client_id, str(user.id))
        print(f"[sync_user_to_nextcloud_host] Checking data path: {user_data_path}")

        if not os.path.exists(user_data_path):
            raise Exception(f"[Host] No data found for user at {user_data_path}")

        print(f"[sync_user_to_nextcloud_host] Data folder exists")
        for item in os.listdir(user_data_path):
            print(f"   - {item}")

        inspect_cmd = f"docker inspect {NC_CONTAINER_NAME}"
        inspect_output = subprocess.check_output(inspect_cmd, shell=True)
        container_info = json.loads(inspect_output)[0]
        mounts = container_info.get("Mounts", [])
        NC_DATA_HOST = next((m["Source"] for m in mounts if m["Destination"] == "/var/www/html"), None)

        if not NC_DATA_HOST:
            raise Exception("Could not determine Nextcloud volume host path.")

        print(f"Host-mounted Nextcloud data dir: {NC_DATA_HOST}")

        nextcloud_user_files = os.path.join(NC_DATA_HOST, "data", username, "files")
        os.makedirs(nextcloud_user_files, exist_ok=True)

        sync_cmd = ["rsync", "-a", "--delete", f"{user_data_path}/", f"{nextcloud_user_files}/"]
        print(f"Running rsync: {' '.join(sync_cmd)}")
        subprocess.run(sync_cmd, check=True)

        print(f"Running OCC files:scan for {username}")
        subprocess.run([
            "docker", "exec", "-u", "www-data", NC_CONTAINER_NAME,
            "php", "occ", "files:scan", username
        ], check=True)

        IntegrationLog.objects.create(
            user=user,
            connector="nextcloud",
            status="success",
            details="Folder sync complete."
        )
        print(f"[sync_user_to_nextcloud_host] Folder synced and scanned for {username}")

    except Exception as e:
        try:
            IntegrationLog.objects.create(
                user_id=user_id,
                connector="nextcloud",
                status="error",
                details=f"Folder sync failed: {str(e)}"
            )
        except Exception:
            pass
        print(f"[sync_user_to_nextcloud_host] ERROR: {str(e)}")
        raise self.retry(exc=e)



# ── Connectors (Google Drive, Dropbox, OneDrive, …): background transfers and webhook delivery ──

@shared_task(name="integrations.run_connector_job", ignore_result=True, soft_time_limit=3600, time_limit=3700)
def run_connector_job(job_id):
    from integrations.connectors import run_job
    from integrations.models import ConnectorJob
    job = ConnectorJob.objects.select_related("user").filter(pk=job_id, status="queued").first()
    if job:
        run_job(job)


@shared_task(bind=True, name="integrations.deliver_webhook", ignore_result=True, max_retries=4, default_retry_delay=30)
def deliver_webhook(self, account_id, event, data):
    """POST one signed event; retried with back-off when the receiver is down (never on a 4xx answer)."""
    from integrations.connectors import deliver
    from integrations.models import ConnectorAccount
    from integrations.providers import ConnectorError
    acc = ConnectorAccount.objects.filter(pk=account_id).first()
    if not acc or not acc.extra.get("webhook_url"):
        return
    try:
        code = deliver(acc, event, data)
    except ConnectorError as exc:
        IntegrationLog.objects.create(user=acc.user, connector=acc.service[:50], status="failed",
                                      details=f"{event}: {exc}")
        return
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30 * (2 ** self.request.retries))
    if code >= 500:
        raise self.retry(countdown=30 * (2 ** self.request.retries))
