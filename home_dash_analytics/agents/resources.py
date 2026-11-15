"""
Resource management agent: is there capacity for more agent work right now?

Watches Celery (workers, active tasks, queue backlog), Redis (memory, evictions), PostgreSQL
(connections in use vs max_connections), the AI provider (does it answer?) and RabbitMQ when one is
configured (RABBITMQ_MANAGEMENT_URL, RABBITMQ_USER, RABBITMQ_PASSWORD). Produces one state:

    healthy   -> the orchestrator dispatches normally
    strained  -> the orchestrator dispatches one run per organisation per cycle
    critical  -> the orchestrator pauses; nothing new is queued

Snapshots are cached for 30 seconds; state changes are emitted as agent events.
"""
from __future__ import annotations

import os

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

from .events import emit

CACHE_KEY = 'agents:resource-snapshot'
STRAINED = {'redis_memory': .75, 'db_connections': .70, 'backlog': 200}
CRITICAL = {'redis_memory': .90, 'db_connections': .90, 'backlog': 1000}


def _redis():
    import redis
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=3)


def _check_redis(report, reasons):
    try:
        r = _redis()
        mem = r.info('memory')
        clients = r.info('clients')
        stats = r.info('stats')
        used, limit = int(mem.get('used_memory', 0)), int(mem.get('maxmemory', 0) or 0)
        ratio = round(used / limit, 3) if limit else None
        queues = {q: r.llen(q) for q in dict.fromkeys([settings.CELERY_TASK_DEFAULT_QUEUE,
                                                        getattr(settings, 'CELERY_FAST_QUEUE', '')]) if q}
        report['redis'] = {'ok': True, 'used_memory': mem.get('used_memory_human'),
                           'maxmemory': mem.get('maxmemory_human'), 'memory_ratio': ratio,
                           'clients': clients.get('connected_clients'), 'evicted_keys': stats.get('evicted_keys')}
        report['queues'] = queues
        backlog = sum(queues.values())
        if ratio is not None and ratio >= CRITICAL['redis_memory']:
            reasons.append(('critical', f'Redis memory at {ratio:.0%}'))
        elif ratio is not None and ratio >= STRAINED['redis_memory']:
            reasons.append(('strained', f'Redis memory at {ratio:.0%}'))
        if backlog >= CRITICAL['backlog']:
            reasons.append(('critical', f'{backlog} tasks waiting in the queues'))
        elif backlog >= STRAINED['backlog']:
            reasons.append(('strained', f'{backlog} tasks waiting in the queues'))
    except Exception as exc:
        report['redis'] = {'ok': False, 'error': type(exc).__name__}
        reasons.append(('critical', 'Redis (task queue) is unreachable'))


def _check_celery(report, reasons):
    try:
        from aiDocuMines.celery_app import app
        insp = app.control.inspect(timeout=1.5)
        replies = insp.ping() or {}
        active = insp.active() or {}
        report['celery'] = {'workers': sorted(replies), 'active_tasks': sum(len(v) for v in active.values())}
        if not replies:
            reasons.append(('critical', 'No Celery worker is answering'))
    except Exception as exc:
        report['celery'] = {'workers': [], 'error': type(exc).__name__}
        reasons.append(('critical', 'Celery workers could not be inspected'))


def _check_postgres(report, reasons):
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity")
            used = cur.fetchone()[0]
            cur.execute("SHOW max_connections")
            limit = int(cur.fetchone()[0])
        ratio = round(used / limit, 3) if limit else None
        report['postgres'] = {'connections': used, 'max_connections': limit, 'ratio': ratio}
        if ratio is not None and ratio >= CRITICAL['db_connections']:
            reasons.append(('critical', f'Database connections at {ratio:.0%}'))
        elif ratio is not None and ratio >= STRAINED['db_connections']:
            reasons.append(('strained', f'Database connections at {ratio:.0%}'))
    except Exception as exc:
        report['postgres'] = {'error': type(exc).__name__}
        reasons.append(('critical', 'The database could not be checked'))


def _check_rabbitmq(report, reasons):
    url = os.getenv('RABBITMQ_MANAGEMENT_URL', '').rstrip('/')
    if not url:
        report['rabbitmq'] = None   # not part of this deployment
        return
    try:
        import requests
        resp = requests.get(f'{url}/api/overview', timeout=3,
                            auth=(os.getenv('RABBITMQ_USER', ''), os.getenv('RABBITMQ_PASSWORD', '')))
        resp.raise_for_status()
        data = resp.json()
        totals = data.get('queue_totals', {})
        report['rabbitmq'] = {'ok': True, 'messages': totals.get('messages'),
                              'unacknowledged': totals.get('messages_unacknowledged'),
                              'connections': (data.get('object_totals') or {}).get('connections')}
        if (totals.get('messages') or 0) >= CRITICAL['backlog']:
            reasons.append(('critical', f"{totals.get('messages')} messages waiting in RabbitMQ"))
        elif (totals.get('messages') or 0) >= STRAINED['backlog']:
            reasons.append(('strained', f"{totals.get('messages')} messages waiting in RabbitMQ"))
    except Exception as exc:
        report['rabbitmq'] = {'ok': False, 'error': type(exc).__name__}
        reasons.append(('critical', 'RabbitMQ is configured but unreachable'))


def provider_reachable(provider: str, endpoint: str | None = None) -> bool:
    """Does the AI provider answer? Cached for 60 seconds per provider."""
    key = f'agents:provider-ok:{provider}:{endpoint or ""}'
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        from core.llm_providers import list_models
        listed = list_models(provider, endpoint)
        ok = bool(listed.get('models') or listed.get('data'))
    except Exception:
        ok = False
    cache.set(key, ok, 60)
    return ok


def assess(force=False) -> dict:
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached
    report, reasons = {}, []
    for check in (_check_redis, _check_celery, _check_postgres, _check_rabbitmq):
        check(report, reasons)
    state = 'critical' if any(l == 'critical' for l, _ in reasons) else 'strained' if reasons else 'healthy'
    report.update(state=state, reasons=[msg for _, msg in reasons])
    previous = cache.get('agents:resource-state')
    if previous != state:
        cache.set('agents:resource-state', state, None)
        if previous is not None or state != 'healthy':
            emit('resource', f'capacity.{state}', severity={'critical': 'critical', 'strained': 'warning'}.get(state, 'info'),
                 reasons=report['reasons'])
    cache.set(CACHE_KEY, report, 150)   # beat refreshes it every minute; pages never wait for a live check
    return report


@shared_task(name='home_dash_analytics.resource_agent', ignore_result=True, soft_time_limit=60, time_limit=90)
def resource_agent():
    return assess(force=True)['state']
