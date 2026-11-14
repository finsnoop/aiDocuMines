"""The agents' shared event log (blackboard): every decision is recorded, never edited."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def emit(agent: str, kind: str, *, tenant_id=None, severity: str = 'info', **payload):
    """Record one agent event. Never raises: losing an event must not stop the agents."""
    from ..models import AgentEvent
    try:
        return AgentEvent.objects.create(agent=agent, kind=kind, tenant_id=tenant_id, severity=severity,
                                         payload=payload)
    except Exception:
        logger.warning('Could not record agent event %s.%s', agent, kind, exc_info=True)
        return None


def recent(tenant_id=None, limit=50, platform=False):
    from ..models import AgentEvent
    qs = AgentEvent.objects.all() if platform else AgentEvent.objects.filter(tenant_id=tenant_id)
    return list(qs.values('created_at', 'agent', 'kind', 'severity', 'payload')[:limit])
