"""
Orchestrator agent: decides what work exists and hands each piece to the specialist that does it best.

Specialists (worker agents):
    ocr           documents with no extracted text (scans, images) -> text
    intelligence  documents with text and no current analysis -> plan, extract, verify (intelligence.py)
    similarity    portfolio changed -> groups of near-identical documents (embeddings), cached

Each cycle (Celery beat, and on demand when a dashboard sees new documents) the orchestrator:
    1. asks the resource agent for capacity: healthy -> 3 runs per organisation, strained -> 1,
       critical -> pause (nothing new is queued);
    2. per organisation, asks the cost agent whether spending is allowed (budget, hard stop),
       checks the organisation's AI policy, model configuration and that the provider answers;
    3. dispatches work per document owner (the agents act with that person's permissions),
       never two active intelligence runs per owner, skipping documents that failed recently;
    4. records every decision as an agent event.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from . import cost, resources
from .events import emit

RUN_SIZE = 20
CAPACITY = {'healthy': 3, 'strained': 1, 'critical': 0}
RETRY_AFTER = timedelta(hours=6)
OCR_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp'}
TICK_DEBOUNCE_SECONDS = 300
OCR_ATTEMPT_TTL = 24 * 3600      # a file is offered to OCR at most once a day, whether or not OCR succeeded


# ── what needs doing ────────────────────────────────────────────────────────
def needs_intelligence(tenant_id=None):
    from core.models import File
    qs = (File.objects.exclude(content__isnull=True).exclude(content='')
          .filter(user__client_id__isnull=False, user__is_active=True)
          .filter(Q(business_intelligence__isnull=True)
                  | Q(business_intelligence__source_updated_at__lt=F('updated_at'))))
    return qs.filter(user__client_id=tenant_id) if tenant_id else qs


def needs_ocr(tenant_id=None):
    from core.models import File
    since = timezone.now() - timedelta(hours=24)
    qs = (File.objects.filter(Q(content__isnull=True) | Q(content=''))
          .filter(user__client_id__isnull=False, user__is_active=True)
          .filter(extension__in=list(OCR_EXTENSIONS))
          .exclude(ocr_versions__created_at__gte=since))     # one OCR attempt per day at most
    return qs.filter(user__client_id=tenant_id) if tenant_id else qs


def recently_failed(tenant_id) -> set:
    from ..models import IntelligenceRun
    failed = set()
    for errors in IntelligenceRun.objects.filter(tenant_id=tenant_id, updated_at__gte=timezone.now() - RETRY_AFTER) \
            .values_list('errors', flat=True):
        failed.update(e['file_id'] for e in errors or [] if isinstance(e, dict) and e.get('file_id') is not None)
    return failed


def model_ready(user) -> tuple[bool, str]:
    """Organisation policy allows AI, a model is configured and the provider answers."""
    from custom_authentication.models import OrganisationPolicy
    from ..intelligence import configured_model
    if OrganisationPolicy.objects.filter(client_id=user.client_id, assistant_enabled=False).exists():
        return False, 'AI is switched off for this organisation.'
    try:
        config = configured_model(user)
    except Exception:
        return False, 'No AI model is configured.'
    if not (config and config.get('provider') and config.get('model')):
        return False, 'No AI model is configured.'
    if not resources.provider_reachable(config['provider'], config.get('endpoint')):
        return False, f"The AI provider ({config['provider']}) is not answering."
    return True, ''


# ── specialists ─────────────────────────────────────────────────────────────
def dispatch_ocr(files) -> int:
    from document_ocr.models import OCRRun
    from document_ocr.tasks import process_ocr
    n = 0
    for f in files:
        # Recorded before queuing: a failing OCR (missing pages, unreadable file) is not re-queued every cycle.
        cache.set(f'agents:ocr-attempt:{f.id}', 1, OCR_ATTEMPT_TTL)
        run = OCRRun.objects.create(project_id=f.project_id, service_id=f.service_id, client=f.user.client,
                                    client_name=f.user.email or 'user', status='Pending', ocr_option='Basic-ocr')
        process_ocr.delay(run.id, f.id, 'Basic-ocr')
        n += 1
    return n


def dispatch_intelligence(owner, tenant_id, file_ids):
    from ..intelligence import build_intelligence
    from ..models import IntelligenceRun
    run = IntelligenceRun.objects.create(user=owner, tenant_id=tenant_id, file_ids=file_ids)
    build_intelligence.delay(str(run.pk))
    return run


@shared_task(name='home_dash_analytics.similarity_agent', ignore_result=True, soft_time_limit=600, time_limit=660)
def similarity_agent(tenant_id, user_id=None):
    """Compute and cache the organisation's near-duplicate groups so dashboards never wait for embeddings.

    Groups are organisation-wide; each viewer is shown only the members they may open (intelligence.similar_groups).
    `user_id` is accepted for tasks queued by older code and ignored.
    """
    from ..intelligence import compute_similar_groups, similarity_is_current
    if similarity_is_current(tenant_id):
        return
    result = compute_similar_groups(tenant_id)
    emit('similarity', 'groups.updated', tenant_id=tenant_id, groups=len(result['groups']))


def stale_similarity_tenants(tenant_id=None) -> list:
    """Organisations whose near-duplicate groups are missing or older than their documents."""
    from core.models import File
    from ..intelligence import similarity_is_current
    tids = [tenant_id] if tenant_id else (File.objects.filter(user__client_id__isnull=False)
                                          .values_list('user__client_id', flat=True).distinct())
    return [t for t in tids if not similarity_is_current(t)]


# ── the cycle ───────────────────────────────────────────────────────────────
@shared_task(name='home_dash_analytics.orchestrator', ignore_result=True, soft_time_limit=300, time_limit=360)
def orchestrate(tenant_id=None) -> dict:
    capacity = resources.assess()
    per_tenant = CAPACITY[capacity['state']]
    if per_tenant == 0:
        emit('orchestrator', 'paused', severity='warning', reasons=capacity['reasons'])
        return {'state': capacity['state'], 'dispatched': 0}

    work = defaultdict(lambda: {'intelligence': defaultdict(list), 'ocr': []})
    owners = {}
    for f in needs_intelligence(tenant_id).select_related('user').only('id', 'user').order_by('id')[:2000]:
        work[f.user.client_id]['intelligence'][f.user_id].append(f.id)
        owners[f.user_id] = f.user
    candidates = list(needs_ocr(tenant_id).select_related('user', 'user__client').order_by('id')[:400])
    tried = cache.get_many([f'agents:ocr-attempt:{f.id}' for f in candidates]) if candidates else {}
    for f in candidates:
        if f'agents:ocr-attempt:{f.id}' not in tried:
            work[f.user.client_id]['ocr'].append(f)

    dispatched = 0
    for tid, jobs in work.items():
        allowed, why = cost.allow(tid)
        if not allowed:
            emit('orchestrator', 'tenant.skipped', tenant_id=tid, severity='warning', reason=why)
            continue
        budget = per_tenant
        # OCR first: text is what the intelligence agent needs next.
        ocr_files = jobs['ocr'][:budget * RUN_SIZE]
        if ocr_files:
            n = dispatch_ocr(ocr_files)
            dispatched += n
            emit('orchestrator', 'dispatch', tenant_id=tid, specialist='ocr', documents=n)
        skip = recently_failed(tid)
        from ..models import IntelligenceRun
        runs = 0
        for owner_id, ids in jobs['intelligence'].items():
            if runs >= budget:
                break
            owner = owners[owner_id]
            if IntelligenceRun.objects.filter(user=owner, status__in=['queued', 'running']).exists():
                continue
            ids = [i for i in ids if i not in skip][:RUN_SIZE]
            if not ids:
                continue
            ready, why = model_ready(owner)
            if not ready:
                emit('orchestrator', 'tenant.skipped', tenant_id=tid, severity='warning', reason=why)
                break       # same model and provider for the whole organisation
            run = dispatch_intelligence(owner, tid, ids)
            runs += 1
            dispatched += len(ids)
            emit('orchestrator', 'dispatch', tenant_id=tid, specialist='intelligence', documents=len(ids),
                 run_id=str(run.pk), owner=owner.email)
    # Similarity needs no model or budget (local embeddings): refresh every organisation whose portfolio
    # changed since its groups were computed, whether or not it had other work this cycle.
    for tid in stale_similarity_tenants(tenant_id):
        similarity_agent.delay(tid)
    cache.set('agents:orchestrator-last', {'at': timezone.now().isoformat(), 'state': capacity['state'],
                                           'dispatched': dispatched}, None)
    return {'state': capacity['state'], 'dispatched': dispatched}


def request_cycle(tenant_id) -> None:
    """Ask for an orchestrator cycle for one organisation, at most every few minutes."""
    try:
        if cache.add(f'agents:cycle:{tenant_id}', 1, timeout=TICK_DEBOUNCE_SECONDS):
            orchestrate.delay(tenant_id)
    except Exception:
        pass  # dashboards must still load when the queue or cache is down


def agents_ready(user) -> tuple[bool, str]:
    try:
        allowed, why = cost.allow(user.client_id)
        if not allowed:
            return False, why
        return model_ready(user)
    except Exception:
        return False, 'Agent status could not be determined.'
