# system_settings/tasks.py

import logging
from celery import shared_task
from django.utils.timezone import now

from custom_authentication.models import CustomUser, Client
from core.models import Run, EndpointResponseTable
from .models import SystemSettings
from .utils import validate_system_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def process_system_settings(self, client_id: int, run_id: str, settings_payload: dict):
    """
    Celery task to validate and save system settings.
    - Links to Run
    - Updates EndpointResponseTable
    """
    try:
        logger.info(f"Saving system settings for client_id={client_id} via run_id={run_id}")

        run = Run.objects.get(run_id=run_id)
        client = Client.objects.get(id=client_id)

        # Validate incoming data
        cleaned_data = validate_system_settings(settings_payload)

        # Save or update SystemSettings
        settings_obj, created = SystemSettings.objects.update_or_create(
            client=client,
            defaults=cleaned_data
        )

        logger.info(f"System settings {'created' if created else 'updated'} for client {client.name}")

        # Save task result in EndpointResponseTable
        EndpointResponseTable.objects.update_or_create(
            run=run,
            client=client,
            endpoint_name="SystemSettingsView",
            defaults={
                "status": "Completed",
                "response_data": {
                    "client_id": client_id,
                    "client_name": client.name,
                    "settings_id": settings_obj.id,
                    "saved_fields": list(cleaned_data.keys()),
                    "timestamp": str(now()),
                },
            },
        )

        # Mark run as completed
        run.status = "Completed"
        run.save(update_fields=["status"])

        return {
            "message": f"System settings saved for client: {client.name}",
            "settings_id": settings_obj.id,
        }

    except Client.DoesNotExist:
        logger.error(f"Client with ID {client_id} not found.")
        return {"error": f"Client with ID {client_id} not found."}

    except Run.DoesNotExist:
        logger.error(f"Run with ID {run_id} not found.")
        return {"error": f"Run with ID {run_id} not found."}

    except Exception as e:
        logger.exception("Failed to save system settings.")
        return {"error": str(e)}

