"""
Tamper evidence for the audit trail: a hash chain per organisation over AdminAuditEvent.

entry_hash = SHA-256(prev_hash + canonical JSON of the event). Editing, inserting or removing an event
anywhere breaks every later link, which verify() reports. Events written before the chain existed have
no hash and are listed separately.
"""
import hashlib
import json


def _canonical(e) -> str:
    return json.dumps({
        "client": e.client_id, "actor": e.actor_id, "action": e.action, "target_type": e.target_type,
        "target_id": e.target_id, "target_label": e.target_label, "details": e.details,
        "ip": e.ip_address, "at": e.created_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(e) -> str:
    return hashlib.sha256((e.prev_hash + _canonical(e)).encode()).hexdigest()


def verify(client_id) -> dict:
    """Walk the organisation's chain from the oldest event; report the first break, if any."""
    from .models import AdminAuditEvent
    prev, checked, unchained = "", 0, 0
    for e in AdminAuditEvent.objects.filter(client_id=client_id).order_by("id").iterator(chunk_size=1000):
        if not e.entry_hash:
            unchained += 1
            continue
        if e.prev_hash != prev or entry_hash(e) != e.entry_hash:
            return {"intact": False, "checked": checked, "broken_at": e.pk, "at": e.created_at, "unchained": unchained}
        prev, checked = e.entry_hash, checked + 1
    return {"intact": True, "checked": checked, "unchained": unchained, "last_hash": prev}
