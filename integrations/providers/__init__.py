"""Registry of connectors. Nextcloud and OnlyOffice keep their own code paths in integrations/views.py."""
from .base import Api, ConnectorError, NotSupported, Provider
from .collab import Notion, Slack
from .crm import HubSpot, Salesforce
from .dev import GitHub, GitLab, Jira
from .esign import AdobeSign, DocuSign
from .storage import Box, Dropbox, GoogleDrive, OneDrive, Teams
from .webhooks import Make, Zapier

PROVIDERS: dict[str, Provider] = {p.key: p for p in (
    GoogleDrive(), Dropbox(), OneDrive(), Box(), Teams(), Slack(), Notion(), GitHub(), GitLab(), Jira(),
    Salesforce(), HubSpot(), Zapier(), Make(), DocuSign(), AdobeSign(),
)}


def get_provider(key: str) -> Provider | None:
    return PROVIDERS.get(key)


__all__ = ["Api", "ConnectorError", "NotSupported", "Provider", "PROVIDERS", "get_provider"]
