"""Industry vertical switches: hidden everywhere when off, refused by the API, configurable only by superusers."""
from core.tests.test_security_audit import Base, api, make_user, token_for

from . import verticals
from .models import VerticalSetting


def client_for(tok):
    return api(tok, HTTP_X_CLIENT_ID=tok.application.client_id)


class VerticalTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_user('root@platform.test', cls.firm_a, staff=True, groups=['Admin'])
        cls.root.is_superuser = True
        cls.root.save()
        cls.app_root, cls.tok_root = token_for(cls.root)

    def switch(self, tenant, **values):
        return client_for(self.tok_root).put('/api/v1/system-settings/auth/admin/verticals/',
                                             {'tenant': tenant.id if tenant else None, 'settings': values},
                                             format='json')

    def catalog(self, tok):
        r = client_for(tok).get('/api/v1/core/bulk/catalog/')
        self.assertEqual(r.status_code, 200)
        return {verticals.vertical_key(s['vertical']) for s in r.json()['services']}

    def test_labels_map_to_one_vertical(self):
        for label, key in [('Class Actions & Mass Claims', 'class_actions'), ('Labor Employment', 'labor_employment'),
                           ('Labor & Employment', 'labor_employment'), ('Regulatory & Privacy (Support)',
                                                                        'regulatory_compliance'),
                           ('IP Litigation', 'ip_litigation'), ('AI Services', 'core'), ('Something new', 'core')]:
            self.assertEqual(verticals.vertical_key(label), key, label)

    def test_everything_is_on_until_configured(self):
        self.assertEqual(verticals.enabled_for(self.a1), {'core', *verticals.OPTIONAL})
        self.assertIn('class_actions', self.catalog(self.tok_a1))

    def test_organisation_switch_hides_and_refuses_only_for_that_organisation(self):
        self.assertEqual(self.switch(self.firm_a, class_actions=False).status_code, 200)
        self.assertNotIn('class_actions', self.catalog(self.tok_a1))
        self.assertIn('private_equity', self.catalog(self.tok_a1))
        self.assertIn('class_actions', self.catalog(self.tok_b1))      # other organisations unaffected
        r = client_for(self.tok_a1).get('/api/v1/class-actions/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['vertical'], 'class_actions')
        self.assertNotEqual(client_for(self.tok_b1).get('/api/v1/class-actions/').status_code, 403)
        # starting a switched-off service through the catalogue is refused too
        r = client_for(self.tok_a1).post('/api/v1/core/bulk/submit/', {
            'job_type': 'ca-evidence-culling', 'project_id': 'p', 'service_id': 's', 'file_ids': [1]}, format='json')
        self.assertEqual(r.status_code, 403)
        # back to inheriting: visible again, nothing was deleted
        self.switch(self.firm_a, class_actions=None)
        self.assertIn('class_actions', self.catalog(self.tok_a1))

    def test_platform_switch_is_the_default_and_onboarding_choices_win(self):
        self.switch(None, ip_litigation=False)
        self.assertNotIn('ip_litigation', self.catalog(self.tok_a1))      # default for everyone
        self.switch(self.firm_b, ip_litigation=True)                      # chosen at onboarding
        self.assertIn('ip_litigation', self.catalog(self.tok_b1))
        self.assertEqual(VerticalSetting.objects.filter(key='ip_litigation').count(), 2)

    def services(self, tok):
        return {s['service_type'] for s in client_for(tok).get('/api/v1/core/bulk/catalog/').json()['services']}

    def test_cherry_picked_features_across_verticals(self):
        """A client that is not one vertical: everything optional off, two features hand-picked from two verticals."""
        self.switch(self.firm_a, **{k: False for k in verticals.OPTIONAL})
        self.switch(self.firm_a, **{'service:ca-pii-redaction': True, 'service:le-wage-hour-analysis': True,
                                    'service:ai-document-translation': False})
        mine = self.services(self.tok_a1)
        self.assertIn('ca-pii-redaction', mine)
        self.assertIn('le-wage-hour-analysis', mine)
        self.assertNotIn('ca-evidence-culling', mine)           # rest of Class Actions stays hidden
        self.assertNotIn('pe-classify-dedup', mine)
        self.assertNotIn('ai-document-translation', mine)       # a core feature can be switched off on its own
        self.assertIn('ai-ocr-extraction', mine)
        # the vertical's API stays reachable for the picked feature; untouched verticals are refused
        self.assertNotEqual(client_for(self.tok_a1).get('/api/v1/class-actions/').status_code, 403)
        self.assertEqual(client_for(self.tok_a1).get('/api/v1/private-equity/').status_code, 403)
        r = client_for(self.tok_a1).post('/api/v1/core/bulk/submit/', {
            'job_type': 'ca-evidence-culling', 'project_id': 'p', 'service_id': 's', 'file_ids': [1]}, format='json')
        self.assertEqual(r.status_code, 403)                    # a feature that was not picked is refused
        self.assertIn('ca-evidence-culling', self.services(self.tok_b1))   # other organisations unaffected

    def test_feature_off_inside_an_enabled_vertical(self):
        self.switch(self.firm_a, **{'service:pe-rfp-builder': False})
        mine = self.services(self.tok_a1)
        self.assertNotIn('pe-rfp-builder', mine)
        self.assertIn('pe-classify-dedup', mine)

    def test_admin_view_lists_every_feature_with_its_switches(self):
        self.switch(self.firm_a, class_actions=False, **{'service:ca-pii-redaction': True})
        data = client_for(self.tok_root).get(
            f'/api/v1/system-settings/auth/admin/verticals/?tenant={self.firm_a.id}').json()
        ca = next(v for v in data['verticals'] if v['key'] == 'class_actions')
        self.assertEqual((ca['tenant_setting'], ca['enabled'], ca['enabled_services']), (False, False, 1))
        pii = next(s for s in ca['services'] if s['service_type'] == 'ca-pii-redaction')
        self.assertEqual((pii['tenant_setting'], pii['enabled']), (True, True))
        from core.service_catalog import ALL_SERVICES
        self.assertEqual(sum(len(v['services']) for v in data['verticals']), len(ALL_SERVICES))   # nothing missing

    def test_dashboard_departments_follow_the_switches(self):
        from unittest.mock import patch
        from home_dash_analytics.intelligence import business_overview
        self.switch(self.firm_a, immigration=False)
        with patch('home_dash_analytics.agents.orchestrator.request_cycle'):
            data = business_overview(self.a1)
        self.assertNotIn('immigration', data['departments'])
        self.assertIn('finance', data['departments'])
        self.assertNotIn('immigration', data['verticals'])

    def test_my_verticals_reports_the_callers_organisation(self):
        self.switch(self.firm_a, finance=False)
        mine = client_for(self.tok_a1).get('/api/v1/system-settings/verticals/').json()
        self.assertNotIn('finance', mine['enabled'])
        theirs = client_for(self.tok_b1).get('/api/v1/system-settings/verticals/').json()
        self.assertIn('finance', theirs['enabled'])

    def test_only_platform_superusers_configure(self):
        for tok in (self.tok_admin, self.tok_a1, self.tok_badmin):
            c = client_for(tok)
            self.assertEqual(c.get('/api/v1/system-settings/auth/admin/verticals/').status_code, 403)
            self.assertEqual(c.put('/api/v1/system-settings/auth/admin/verticals/',
                                   {'tenant': self.firm_a.id, 'settings': {'finance': False}},
                                   format='json').status_code, 403)
        self.assertFalse(VerticalSetting.objects.exists())
        r = client_for(self.tok_root).get(f'/api/v1/system-settings/auth/admin/verticals/?tenant={self.firm_b.id}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['tenant']['name'], 'Firm B')

    def test_core_vertical_and_unknown_keys_are_refused(self):
        self.assertEqual(self.switch(self.firm_a, core=False).status_code, 400)
        self.assertEqual(self.switch(self.firm_a, **{'service:no-such-thing': False}).status_code, 400)
        self.assertEqual(self.switch(self.firm_a, finance='off').status_code, 400)
        self.assertFalse(VerticalSetting.objects.exists())

    def test_changes_are_audited(self):
        from custom_authentication.models import AdminAuditEvent
        self.switch(self.firm_a, private_equity=False)
        event = AdminAuditEvent.objects.get(action='verticals.updated')
        self.assertEqual((event.actor_id, event.client_id), (self.root.id, self.firm_a.id))
