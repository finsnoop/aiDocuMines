"""
Industry verticals: which services, assistant presets, workflows and dashboard sections an organisation sees.

Every catalogue entry already names its vertical ("Private Equity", "Class Actions & Mass Claims", ...).
`vertical_key()` maps those labels (and the frontend's spellings) to one key below. Adding an industry
means adding one entry here and labelling its services; nothing is deleted when a vertical is off:
it is hidden from the organisation and its APIs refuse requests (VerticalAccessMiddleware).

Switches (system_settings.models.VerticalSetting) exist per vertical and per service ("service:<service_type>"),
platform-wide and per organisation. The most specific one wins:
    organisation + service  >  organisation + vertical  >  platform + service  >  platform + vertical  >  on
so a client can be strictly one vertical, a mix of verticals, or hand-picked features from several.
The core vertical (OCR, translation, anonymisation, search ...) cannot be switched off as a whole, but its
individual services can.
"""
from __future__ import annotations

import re

from django.core.cache import cache

CORE = 'core'

VERTICALS = [
    {'key': CORE, 'label': 'Core document services', 'industry': 'All industries', 'always_on': True,
     'description': 'OCR, translation, anonymisation, search, redlining, versioning, automation, Q&A.',
     'prefixes': ('ai services', 'core')},
    {'key': 'private_equity', 'label': 'Private Equity & M&A', 'industry': 'Legal',
     'description': 'Diligence workroom, closing automation, panel management, post-close tracking.',
     'prefixes': ('private equity',), 'api': '/api/v1/private-equity/'},
    {'key': 'class_actions', 'label': 'Class Actions & Mass Claims', 'industry': 'Legal',
     'description': 'Evidence culling, PII redaction, claim triage, damages and settlements.',
     'prefixes': ('class action',), 'api': '/api/v1/class-actions/'},
    {'key': 'labor_employment', 'label': 'Labour & Employment', 'industry': 'Legal',
     'description': 'Communications review, wage and hour analysis, policy comparison.',
     'prefixes': ('labor', 'labour'), 'api': '/api/v1/labor-employment/'},
    {'key': 'ip_litigation', 'label': 'IP Litigation', 'industry': 'Legal',
     'description': 'Patent analysis, prior art search, infringement assessment.',
     'prefixes': ('ip ', 'ip-', 'intellectual property'), 'api': '/api/v1/ip-litigation/'},
    {'key': 'regulatory_compliance', 'label': 'Regulatory Compliance & Privacy', 'industry': 'Legal',
     'description': 'DSARs, regulatory redaction, gap analysis, privacy compliance.',
     'prefixes': ('regulatory',), 'api': '/api/v1/regulatory-compliance/'},
    {'key': 'immigration', 'label': 'Immigration', 'industry': 'Immigration services',
     'description': 'Passports, visas, CVs, motivation letters, proof of address on the dashboards.',
     'prefixes': ('immigration',)},
    {'key': 'finance', 'label': 'Finance & Accounting', 'industry': 'Finance',
     'description': 'Invoices, payslips, VAT, paid and pending amounts on the dashboards.',
     'prefixes': ('finance', 'accounting')},
]
BY_KEY = {v['key']: v for v in VERTICALS}
OPTIONAL = [v['key'] for v in VERTICALS if not v.get('always_on')]
API_PREFIXES = [(v['api'], v['key']) for v in VERTICALS if v.get('api')]

_CACHE_SECONDS = 300


def vertical_key(label) -> str:
    """The vertical a catalogue label belongs to. Unknown labels count as core (never hidden by accident)."""
    text = re.sub(r'\s+', ' ', str(label or '').strip().lower())
    if text in BY_KEY:
        return text
    for v in VERTICALS:
        if any(text.startswith(p) for p in v['prefixes']):
            return v['key']
    return CORE


def _version():
    try:
        return cache.get_or_set('verticals:version', 1, None)
    except Exception:
        return 0


def invalidate():
    """Called after any change: every organisation's cached view is rebuilt on next use."""
    try:
        cache.incr('verticals:version')
    except Exception:
        try:
            cache.set('verticals:version', 2, None)
        except Exception:
            pass


SERVICE_PREFIX = 'service:'


def _catalog():
    from core.service_catalog import ALL_SERVICES
    return ALL_SERVICES


def _rows(tid):
    """({key: enabled} platform, {key: enabled} organisation)."""
    from .models import VerticalSetting
    from django.db.models import Q
    platform, own = {}, {}
    q = Q(tenant__isnull=True) | Q(tenant_id=tid) if tid is not None else Q(tenant__isnull=True)
    for t, key, enabled in VerticalSetting.objects.filter(q).values_list('tenant_id', 'key', 'enabled'):
        (own if t is not None else platform)[key] = enabled
    return platform, own


def _resolve(platform, own, vertical, service_type=None):
    """Most specific switch wins (see module docstring)."""
    skey = SERVICE_PREFIX + service_type if service_type else None
    vertical_locked = vertical == CORE
    for table, key in ((own, skey), (own, None if vertical_locked else vertical),
                       (platform, skey), (platform, None if vertical_locked else vertical)):
        if key and key in table:
            return table[key]
    return True


def access_for_tenant(tid) -> dict:
    """{'verticals': vertical keys switched on, 'services': service_types switched on,
        'any': verticals with at least one service on (their APIs stay reachable)}"""
    key = f'verticals:{_version()}:{tid}'
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return {k: set(v) for k, v in cached.items()}
    platform, own = _rows(tid)
    on_verticals = {CORE} | {k for k in OPTIONAL if _resolve(platform, own, k)}
    services, any_on = set(), set(on_verticals)
    for s in _catalog():
        v = vertical_key(s.get('vertical'))
        if _resolve(platform, own, v, s['service_type']):
            services.add(s['service_type'])
            any_on.add(v)
    result = {'verticals': on_verticals, 'services': services, 'any': any_on}
    try:
        cache.set(key, {k: sorted(v) for k, v in result.items()}, _CACHE_SECONDS)
    except Exception:
        pass
    return result


def enabled_for_tenant(tid) -> set:
    """Verticals with anything switched on for the organisation (core always included)."""
    return access_for_tenant(tid)['any']


def enabled_for(user) -> set:
    from core.access import tenant_id
    return enabled_for_tenant(tenant_id(user))


def access_for(user) -> dict:
    from core.access import tenant_id
    return access_for_tenant(tenant_id(user))


def vertical_switched_on(user, key) -> bool:
    """The vertical itself is on (dashboard sections that are not individual services, e.g. Immigration)."""
    return key in access_for(user)['verticals']


def is_enabled(user, label_or_key) -> bool:
    """A vertical (by label or key) has at least one service on for the user's organisation."""
    return vertical_key(label_or_key) in enabled_for(user)


def service_enabled(user, service) -> bool:
    return service.get('service_type') in access_for(user)['services']


def filter_services(user, services):
    """Catalogue entries switched on for the user's organisation (the entries themselves are untouched)."""
    on = access_for(user)['services']
    return [s for s in services if s.get('service_type') in on]


def vertical_for_path(path: str):
    for prefix, key in API_PREFIXES:
        if path.startswith(prefix):
            return key
    return None


def describe(tid=None) -> list:
    """Registry with every service and its switches, platform-wide or for one organisation (settings screen)."""
    platform, own = _rows(tid)
    if tid is None:
        own = {}
    out = []
    by_vertical = {}
    for s in _catalog():
        by_vertical.setdefault(vertical_key(s.get('vertical')), []).append(s)
    for v in VERTICALS:
        k = v['key']
        locked = bool(v.get('always_on'))
        row = {f: v[f] for f in ('key', 'label', 'industry', 'description')}
        row['always_on'] = locked
        row['platform_setting'] = None if locked else platform.get(k)          # None = default (on)
        row['tenant_setting'] = None if locked or tid is None else own.get(k)  # None = inherits
        row['enabled'] = _resolve(platform, own, k)
        row['services'] = [{
            'service_type': s['service_type'], 'display_name': s.get('display_name') or s['service_type'],
            'category': s.get('category', ''), 'description': s.get('description', ''),
            'platform_setting': platform.get(SERVICE_PREFIX + s['service_type']),
            'tenant_setting': own.get(SERVICE_PREFIX + s['service_type']) if tid is not None else None,
            'enabled': _resolve(platform, own, k, s['service_type']),
        } for s in sorted(by_vertical.get(k, []), key=lambda s: (s.get('category', ''), s.get('display_name', '')))]
        row['enabled_services'] = sum(1 for s in row['services'] if s['enabled'])
        out.append(row)
    return out
