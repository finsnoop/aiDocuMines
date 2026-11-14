"""Small synthetic model evaluation; not a substitute for a representative legal corpus."""
import json
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from home_dash_analytics.intelligence import SYSTEM, agent_call, configured_model, validate_facts


class Command(BaseCommand):
    help = 'Evaluate configured AI extraction/verification using synthetic, labelled cases (makes paid AI calls).'

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True, help='Actor email; that organisation AI policy and accounting apply.')
        parser.add_argument('--dataset', default='home_dash_analytics/evals/synthetic.json')
        parser.add_argument('--output', default='logs/dashboard-model-evaluation.json')

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(email=options['user'], is_active=True).first()
        if not user or not user.client_id:
            raise CommandError('An active organisation user is required.')
        cases = json.loads(Path(options['dataset']).read_text())
        if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
            raise CommandError('An evaluation dataset must contain 1 to 20 cases.')
        config = configured_model(user)
        results, correct, expected_count, unsupported, returned_count = [], 0, 0, 0, 0
        for case in cases:
            try:
                source = case['source']
                if not isinstance(source, str) or len(source) > 12000:
                    raise ValueError()
                proposed = agent_call(user, config, [{'role':'system','content':SYSTEM},
                    {'role':'user','content':json.dumps({'source':source,'rules':[]})}], 'dashboard.eval.extract')
                verified = agent_call(user, config, [{'role':'system','content':SYSTEM + '\nVerify the proposed facts; retain only supported facts.'},
                    {'role':'user','content':json.dumps({'source':source,'rules':[],'proposed':proposed})}], 'dashboard.eval.verify')
                facts = {key:value for key,value in validate_facts(verified, source).items()
                         if key in proposed and isinstance(proposed[key],dict)
                         and value['value'] == str(proposed[key].get('value')) and key != 'compliance'}
                actual = {key:value['value'] for key,value in facts.items()}
                expected = case['expected']
                hits = sum(actual.get(key) == str(value) for key,value in expected.items())
                extras = sum(key not in expected or actual[key] != str(expected[key]) for key in actual)
                correct += hits
                expected_count += len(expected)
                unsupported += extras
                returned_count += len(actual)
                results.append({'id':case['id'],'matched':hits,'expected':len(expected),
                                'unsupported_or_incorrect':extras,'passed':actual == expected})
            except Exception:
                results.append({'id':case.get('id'),'passed':False,'error':'Provider or structured-output failure'})
        report = {'provider':config['provider'],'model':config['model'], 'scope':'Synthetic extraction/verification component evaluation',
                  'cases':results,'field_precision':correct/returned_count if returned_count else 0,
                  'field_recall':correct/expected_count if expected_count else 0,
                  'unsupported_or_incorrect':unsupported,'passed':all(r['passed'] for r in results)}
        path = Path(options['output'])
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2))
        self.stdout.write(f'Evaluation report: {path}; passed={report["passed"]}')
        if not report['passed']:
            raise CommandError('Model evaluation failed. Do not treat this configuration as production-validated.')
