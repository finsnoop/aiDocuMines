"""The agent system: orchestrator, resource agent, cost agent, usage recording and their API."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache

from core.tests.test_security_audit import Base, api
from core.tests.test_tenancy import make_file, make_user, token_for
from cost_centre.models import EventLog
from .agents import cost, orchestrator, resources, usage
from .models import AgentEvent, AIBudget, IntelligenceRun, ModelPrice

HEALTHY = {'state': 'healthy', 'reasons': []}


def spent(user, model='gpt-test', prompt=0, completion=0, total=None, task='dashboard.extract'):
    return usage.record(user=user, provider='openai', model=model, task=task, prompt_tokens=prompt,
                        completion_tokens=completion, total_tokens=total)


class CostAgentTests(Base):
    def setUp(self):
        super().setUp()
        ModelPrice.objects.create(provider='*', model='gpt-test', input_usd_per_million=Decimal('2'),
                                  output_usd_per_million=Decimal('8'), source='test')

    def test_tokens_become_dollars_per_model_and_task(self):
        spent(self.a1, prompt=1_000_000, completion=500_000)          # 2 + 4 = 6 USD
        result = cost.assess(self.firm_a.id, emit_changes=False)
        self.assertEqual(result['usd'], '6.00')
        self.assertFalse(result['estimated'])
        self.assertEqual(result['breakdown'][0]['task'], 'dashboard.extract')
        self.assertEqual(result['tokens'], 1_500_000)

    def test_totals_without_a_split_are_estimated_not_hidden(self):
        spent(self.a1, total=1_000_000)                                # mean price: 5 USD
        result = cost.assess(self.firm_a.id, emit_changes=False)
        self.assertEqual(result['usd'], '5.00')
        self.assertTrue(result['estimated'])

    def test_models_without_a_price_are_reported_as_unpriced(self):
        spent(self.a1, model='mystery-model', prompt=1000, completion=1000)
        result = cost.assess(self.firm_a.id, emit_changes=False)
        self.assertEqual(result['usd'], '0.00')
        self.assertEqual(result['unpriced_tokens'], 2000)

    def test_spend_never_mixes_organisations(self):
        spent(self.b1, prompt=1_000_000)
        self.assertEqual(cost.assess(self.firm_a.id, emit_changes=False)['tokens'], 0)

    def test_budget_states_and_hard_stop(self):
        AIBudget.objects.create(tenant=self.firm_a, monthly_usd=Decimal('10'), alert_percent=50)
        spent(self.a1, prompt=3_000_000)                               # 6 USD = 60 %
        self.assertEqual(cost.assess(self.firm_a.id)['state'], 'warning')
        self.assertTrue(cost.allow(self.firm_a.id)[0])
        spent(self.a1, prompt=2_000_000)                               # 10 USD = 100 %
        self.assertEqual(cost.assess(self.firm_a.id)['state'], 'exhausted')
        allowed, why = cost.allow(self.firm_a.id)
        self.assertFalse(allowed)
        self.assertIn('budget', why)
        kinds = list(AgentEvent.objects.filter(tenant=self.firm_a, agent='cost').values_list('kind', flat=True))
        self.assertIn('budget.warning', kinds)
        self.assertIn('budget.exhausted', kinds)

    @patch('home_dash_analytics.intelligence.configured_model', return_value={'provider': 'litellm', 'model': 'gpt-test'})
    def test_agents_cannot_spend_past_an_exhausted_budget(self, _):
        from .intelligence import agent_call
        AIBudget.objects.create(tenant=self.firm_a, monthly_usd=Decimal('1'))
        spent(self.a1, prompt=1_000_000)
        with self.assertRaisesRegex(ValueError, 'budget'):
            agent_call(self.a1, {'provider': 'litellm', 'model': 'gpt-test', 'endpoint': 'http://x'}, [], 'dashboard.plan')


class UsageRecordingTests(Base):
    def test_calls_are_recorded_against_the_actor(self):
        with usage.acting_as(self.a2):
            usage.record_response(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                                                        total_tokens=15)),
                                  provider='openai', model='m', task='assistant.chat')
        row = EventLog.objects.get()
        self.assertEqual((row.user_id, row.tenant_id, row.tokens_used), (self.a2.id, self.firm_a.id, 15))
        self.assertEqual(row.metadata['completion_tokens'], 5)

    def test_unattributed_calls_are_logged_not_dropped(self):
        usage.record_response({'prompt_eval_count': 7, 'eval_count': 3}, provider='ollama', model='m', task='x')
        self.assertFalse(EventLog.objects.exists())
        event = AgentEvent.objects.get(kind='usage.unattributed')
        self.assertEqual(event.payload['tokens'], 10)

    def test_callers_that_record_themselves_are_not_counted_twice(self):
        with usage.acting_as(self.a1), usage.recorded_by_caller():
            usage.record_response(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                                                        total_tokens=2)),
                                  provider='openai', model='m', task='t')
        self.assertFalse(EventLog.objects.exists())

    def test_the_shared_model_helper_records_usage(self):
        from grid_documents_interrogation.utils import _openai_chat_completion
        reply = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=20, completion_tokens=4, total_tokens=24),
                                choices=[])
        client = SimpleNamespace(base_url='https://api.openai.com/v1',
                                 chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply)))
        with usage.acting_as(self.a1):
            _openai_chat_completion(client, model='gpt-test', messages=[], max_tokens=10, temperature=0, timeout=5)
        row = EventLog.objects.get()
        self.assertEqual((row.metadata['provider'], row.metadata['task'], row.tokens_used), ('openai', 'assistant.chat', 24))


class ResourceAgentTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _assess(self, **reports):
        def check(name):
            def run(report, reasons):
                level, msg = reports.get(name, (None, None))
                report[name] = {}
                if level:
                    reasons.append((level, msg))
            return run
        with patch.object(resources, '_check_cpu', check('cpu')), \
             patch.object(resources, '_check_redis', check('redis')), \
             patch.object(resources, '_check_celery', check('celery')), \
             patch.object(resources, '_check_postgres', check('postgres')), \
             patch.object(resources, '_check_rabbitmq', check('rabbitmq')):
            return resources.assess(force=True)

    def test_states(self):
        self.assertEqual(self._assess()['state'], 'healthy')
        strained = self._assess(postgres=('strained', 'Database connections at 75%'))
        self.assertEqual(strained['state'], 'strained')
        critical = self._assess(celery=('critical', 'No Celery worker is answering'))
        self.assertEqual(critical['state'], 'critical')
        self.assertIn('No Celery worker is answering', critical['reasons'])
        self.assertTrue(AgentEvent.objects.filter(agent='resource', kind='capacity.critical').exists())

    def test_cpu_load_throttles_then_pauses_the_agents(self):
        for load, state in ((2.0, None), (5.5, 'strained'), (9.0, 'critical')):
            report, reasons = {}, []
            with patch('os.getloadavg', return_value=(load, load, load)), patch('os.cpu_count', return_value=6):
                resources._check_cpu(report, reasons)
            self.assertEqual(reasons[0][0] if reasons else None, state, load)
        # a short spike alone (1-minute load high, 5-minute load low) does not pause them
        report, reasons = {}, []
        with patch('os.getloadavg', return_value=(9.0, 2.0, 2.0)), patch('os.cpu_count', return_value=6):
            resources._check_cpu(report, reasons)
        self.assertEqual(reasons, [])

    def test_rabbitmq_is_only_checked_when_configured(self):
        report, reasons = {}, []
        with patch.dict('os.environ', {'RABBITMQ_MANAGEMENT_URL': ''}):
            resources._check_rabbitmq(report, reasons)
        self.assertIsNone(report['rabbitmq'])
        self.assertEqual(reasons, [])


class OrchestratorTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.file_a1.content = 'MASTER SERVICES AGREEMENT. The contract value is ZAR 2 400 000.'
        self.file_a1.save()

    def cycle(self, capacity=HEALTHY, ready=(True, '')):
        with patch.object(resources, 'assess', return_value=capacity), \
             patch.object(orchestrator, 'model_ready', return_value=ready), \
             patch('home_dash_analytics.intelligence.build_intelligence.delay') as delay, \
             patch.object(orchestrator.similarity_agent, 'delay') as sim, \
             patch('document_ocr.tasks.process_ocr.delay') as ocr:
            result = orchestrator.orchestrate()
        return result, delay, ocr, sim

    def test_new_documents_go_to_the_intelligence_agent(self):
        result, delay, _, sim = self.cycle()
        run = IntelligenceRun.objects.get()
        self.assertEqual((run.user_id, run.tenant_id), (self.a1.id, self.firm_a.id))
        delay.assert_called_once_with(str(run.pk))
        sim.assert_any_call(self.firm_a.id)      # every organisation whose portfolio changed is refreshed
        self.assertTrue(AgentEvent.objects.filter(agent='orchestrator', kind='dispatch', tenant=self.firm_a).exists())
        # one active run per owner
        self.cycle()
        self.assertEqual(IntelligenceRun.objects.count(), 1)

    def test_scanned_documents_go_to_the_ocr_agent_first(self):
        scan = make_file(self.a2, 'scan.pdf')
        scan.extension = 'pdf'
        scan.content = ''
        scan.save()
        _, _, ocr, _ = self.cycle()
        self.assertEqual(ocr.call_args[0][1], scan.id)

    def test_failing_ocr_is_not_requeued_every_cycle(self):
        scan = make_file(self.a2, 'scan.pdf')
        scan.extension = 'pdf'
        scan.content = ''
        scan.save()
        _, _, ocr, _ = self.cycle()
        self.assertEqual(ocr.call_count, 1)
        # OCR failed (no text, no OCR version): the next cycles leave the file alone for a day
        for _ in range(3):
            _, _, ocr, _ = self.cycle()
            self.assertEqual(ocr.call_count, 0)

    def test_critical_capacity_pauses_everything(self):
        result, delay, ocr, _ = self.cycle(capacity={'state': 'critical', 'reasons': ['Redis memory at 95%']})
        self.assertEqual(result['dispatched'], 0)
        delay.assert_not_called()
        self.assertTrue(AgentEvent.objects.filter(kind='paused').exists())

    def test_strained_capacity_throttles_to_one_run(self):
        self.file_a2 = make_file(self.a2, 'second.pdf')
        self.file_a2.content = 'SUPPLY AGREEMENT. Liability shall not exceed ZAR 5 000 000.'
        self.file_a2.save()
        self.cycle(capacity={'state': 'strained', 'reasons': ['Database connections at 75%']})
        self.assertEqual(IntelligenceRun.objects.count(), 1)

    def test_unreachable_model_or_exhausted_budget_skip_the_organisation(self):
        self.cycle(ready=(False, 'The AI provider (litellm) is not answering.'))
        self.assertFalse(IntelligenceRun.objects.exists())
        self.assertTrue(AgentEvent.objects.filter(kind='tenant.skipped', payload__reason__icontains='not answering').exists())
        ModelPrice.objects.create(model='gpt-test', input_usd_per_million=1, output_usd_per_million=1)
        AIBudget.objects.create(tenant=self.firm_a, monthly_usd=Decimal('1'))
        spent(self.a1, prompt=2_000_000)
        self.cycle()
        self.assertFalse(IntelligenceRun.objects.exists())

    def test_recent_failures_are_not_retried_in_a_loop(self):
        IntelligenceRun.objects.create(tenant=self.firm_a, user=self.a1, file_ids=[self.file_a1.id], status='failed',
                                       errors=[{'file_id': self.file_a1.id, 'error': 'provider down'}])
        self.cycle()
        self.assertEqual(IntelligenceRun.objects.count(), 1)


class AgentOperationsApiTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.patches = [patch.object(resources, 'assess', return_value={'state': 'healthy', 'reasons': [],
                                                                        'redis': {'used_memory': '1M'}}),
                        patch.object(orchestrator, 'model_ready', return_value=(True, ''))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_organisation_admins_see_their_organisation_only(self):
        AgentEvent.objects.create(agent='cost', kind='budget.warning', tenant=self.firm_b)
        r = api(self.tok_admin).get('/api/v1/home-analytics/agents/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('redis', r.json()['resources'])              # no platform internals
        self.assertNotIn('tenants', r.json())
        self.assertEqual(r.json()['events'], [])                      # nothing from Firm B
        self.assertEqual(api(self.tok_a1).get('/api/v1/home-analytics/agents/').status_code, 403)

    def test_budget_is_set_by_the_organisation_admin(self):
        r = api(self.tok_admin).put('/api/v1/home-analytics/agents/budget/', {'monthly_usd': '250', 'alert_percent': 75},
                                    format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AIBudget.objects.get(tenant=self.firm_a).monthly_usd, Decimal('250.00'))
        self.assertEqual(api(self.tok_a1).put('/api/v1/home-analytics/agents/budget/', {'monthly_usd': '1'},
                                              format='json').status_code, 403)
        self.assertEqual(api(self.tok_admin).put('/api/v1/home-analytics/agents/budget/', {'monthly_usd': '-5'},
                                                 format='json').status_code, 400)

    def test_only_platform_administrators_set_prices(self):
        body = {'model': 'gpt-test', 'input_usd_per_million': '1.5', 'output_usd_per_million': '6', 'source': 'test'}
        self.assertEqual(api(self.tok_admin).put('/api/v1/home-analytics/agents/prices/', body, format='json').status_code, 403)
        root = make_user('root@platform.test', self.firm_a, superuser=True, staff=True)
        _, tok = token_for(root)
        self.assertEqual(api(tok).put('/api/v1/home-analytics/agents/prices/', body, format='json').status_code, 200)
        self.assertEqual(ModelPrice.objects.get(model='gpt-test').output_usd_per_million, Decimal('6'))
        r = api(tok).get('/api/v1/home-analytics/agents/')
        self.assertIn('tenants', r.json())
        self.assertIn('redis', r.json()['resources'])
