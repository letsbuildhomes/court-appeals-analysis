"""Experiment definitions, planning, read-through reuse, capped runs and finalization; no network."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import appeal_runner
import appeals
from appeal_cohorts import create_cohort, read_cohort, select_cases
from appeal_experiments import create_experiment, read_experiment
from appeal_runtime import digest, read, save

PROMPTS = {k: k.upper() for k in ('system', 'triage', 'extract', 'primer', 'review', 'duration')}
TRIAGE = {'schema_version': 'v5-triage', 'municipal_appeal': 'yes', 'housing': 'yes', 'documents': []}
ENV = {'APPEALS_API_BASE_URL': 'http://127.0.0.1:9', 'APPEALS_API_KEY': 'fake'}


def candidate():
    return {'schema_version': 'v6', 'dockets': [{
        'docket': 'A', 'related_dockets': [],
        'project': {'description': None, 'municipality': None, 'proposed_units': None, 'proposed_lots': None, 'evidence': []},
        'municipal_decision': {'description': None, 'date': None, 'outcome': 'unclear', 'evidence': []},
        'appellants': [],
        'discretion': {'description': None, 'evidence': [], 'involved': 'unclear', 'central': 'unclear', 'central_evidence': []},
        'timeline': [], 'observed_status': {'litigation': 'unclear', 'permission': 'unclear', 'description': None, 'evidence': []},
        'restrictions': [], 'housing_changes': []}]}


class Model:
    """Fake chat model; ``break_review`` makes reviews of those groups omit a member."""
    def __init__(self, break_review=()):
        self.break_review, self.requests = set(break_review), []

    def __call__(self, *args, **kwargs):
        request = kwargs['json']
        self.requests.append(request)
        messages = request['messages']
        if messages[0]['content'].startswith('PRIMER'):
            if len(messages) == 2:
                cases = json.loads(messages[1]['content'])['cases']
                groups = [c['group'] for c in cases]
                if self.break_review & set(groups):
                    groups = groups[:1]
                body = {'reviews': [{'group': g, 'review': {'schema_version': 'v6-review-1', 'scope': {'verdict': 'agree'},
                                                             'changes': [], 'flags': []}} for g in groups]}
            else:
                cases = json.loads(messages[-1]['content'])['cases']
                body = {'groups': [{'group': c['group'], 'dockets': [{'docket': 'A', 'start': None, 'end': None,
                                                                      'note': 'No supported bounds'}]} for c in cases]}
        else:
            body = TRIAGE if len(messages) == 2 else candidate()
        response = Mock(status_code=200, text=json.dumps(body))
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}],
                                      'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}
        return response


def passthrough():
    """Accept fake triage and extraction answers as valid."""
    return (patch('group_scout.validate_triage', side_effect=lambda raw, pages, version: (json.loads(raw), [])),
            patch('group_scout.route_triage', return_value={'route': 'extract'}),
            patch('group_scout.validate_extract', side_effect=lambda raw, pages, version: (json.loads(raw), [])))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runs = self.root/'runs'
        self.base = self.runs/'base'
        self.experiments = self.root/'experiments'
        self.definitions = self.root/'definitions'
        self.posts = 0
        self.build_base()
        self.artifact = self.root/'artifact'
        create_experiment(self.base, self.artifact)

    def run_engine(self, args, model, **options):
        validators = passthrough()
        with patch('appeal_runtime.httpx.Client') as http, patch.dict('os.environ', ENV), \
                validators[0], validators[1], validators[2]:
            post = http.return_value.__enter__.return_value.post
            post.side_effect = model
            result = options.pop('call')(args, **options) if 'call' in options else appeals.run(args, **options)
            self.posts = post.call_count
        return result

    def build_base(self):
        """A real engine run in which review batch (c, d) failed."""
        groups = []
        save(self.base/'prompts.json', PROMPTS)
        for gid in 'abcd':
            source = f'<page number="1">Source {gid}</page>'
            (self.base/'sources').mkdir(parents=True, exist_ok=True)
            (self.base/'sources'/f'{gid}.txt').write_text(source)
            groups.append({'group_id': gid, 'source_sha256': digest(source), 'eligible': True, 'documents': [], 'dockets': []})
        save(self.base/'manifest.json', {'schema_version': 'appeals-1', 'name': 'base', 'created_at': '2026-01-01T00:00:00+00:00',
             'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536, 'max_chars': 600000,
             'review_batch_size': 2, 'review_batch_chars': 200000,
             'prompt_sha256': digest(json.dumps(PROMPTS, sort_keys=True)), 'groups': groups})
        self.run_engine(self.args(self.runs, 'base', 1), Model(break_review={'c'}))
        self.assertEqual(read(self.base/'results/c.json')['status'], 'review_failed')

    def args(self, root, name, max_spend, **options):
        return SimpleNamespace(root=root, name=name, max_spend=max_spend, price_multiplier=1, workers=1, max_chars=None,
                               first_pass_only=False, api_base_url=None, api_url=None, retry_failed=False,
                               retry_max_output_tokens=None, **options)

    def define(self, name, cohort, **definition):
        folder = self.definitions/name
        folder.mkdir(parents=True)
        create_cohort(self.artifact, folder/'cohort.json', cohort, name='test', description='Test cohort')
        save(folder/'experiment.json', {'schema_version': 'appeal-experiment-definition-1', 'hypothesis': 'Test',
                                        'base_run': str(self.base), 'cohort': 'cohort.json', **definition})
        return folder

    def plan(self, name):
        validators = passthrough()
        with patch('appeal_runtime.httpx.Client') as http, validators[0], validators[1], validators[2]:
            record = appeal_runner.plan(SimpleNamespace(root=self.experiments, definitions=self.definitions,
                                                        name=name, price_multiplier=1))
            http.assert_not_called()
        return record

    def execute(self, name, max_spend, model=None):
        args = SimpleNamespace(root=self.experiments, name=name, max_spend=max_spend, price_multiplier=1, workers=1,
                               first_pass_only=False, api_base_url=None, api_url=None, retry_failed=False,
                               retry_max_output_tokens=None)
        return self.run_engine(args, model or Model(), call=appeal_runner.execute)

    def test_cohort_saves_query_identities_and_hashes(self):
        create_cohort(self.artifact, self.root/'cohort.json', {'status': ['review_failed']}, name='t', description='d')
        cohort = read_cohort(self.root/'cohort.json')
        self.assertEqual([c['case_id'] for c in cohort['cases']], ['c', 'd'])
        self.assertEqual(cohort['cases'][0]['source_sha256'], digest('<page number="1">Source c</page>'))
        with self.assertRaises(FileExistsError):
            create_cohort(self.artifact, self.root/'cohort.json', {'status': ['review_failed']}, name='t', description='d')
        cases = read_experiment(self.artifact)['cases']
        self.assertEqual(select_cases(cases, {'duration_kind': ['unknown']}), ['a', 'b'])
        sample = {'scope': ['confirmed'], 'sample': 2, 'seed': 7}
        self.assertEqual(select_cases(cases, sample), select_cases(cases, sample))
        for query in ({}, {'sample': 2}, {'status': 'review_failed'}, {'unknown': ['x']}, {'case': ['absent']}):
            with self.assertRaises(ValueError):
                select_cases(cases, query)

    def test_plan_composes_definition_adds_companions_and_estimates_without_calls(self):
        self.define('retry', {'case': ['c']})
        record = self.plan('retry')
        self.assertEqual(record['cases'], {'selected': ['c'], 'companions': ['d']})
        self.assertEqual(record['batching']['status'], 'preserved')
        self.assertEqual(record['variables']['recipe'], [])
        # Triage and extraction are identical and inherited; the failed review is not.
        self.assertEqual(record['estimate']['new_calls'], {'review': 1, 'duration (unreached)': 1})
        self.assertGreater(record['estimate']['inherited_usd'], 0)
        run = self.experiments/'retry'/'run'
        self.assertFalse((run/'calls').exists())  # The dry run used a scratch directory.
        self.assertEqual(read(run/'recipe.json')['steps']['review']['component'], 'review')
        self.assertTrue((self.experiments/'retry'/'definition'/'experiment.json').exists())
        with self.assertRaises(ValueError):
            self.plan('retry')  # Never replaced.

    def test_overrides_compose_prompts_settings_batching_and_components(self):
        folder = self.define('variant', {'case': ['a', 'b']})
        (folder/'prompts').mkdir()
        (folder/'prompts'/'review.md').write_text('PRIMER-COMPATIBLE NEW REVIEW')
        save(folder/'recipe.json', {'steps': {'review': {'settings': {'max_output_tokens': 131072}}}})
        record = self.plan('variant')
        self.assertEqual(list(record['variables']['prompts']), ['review'])
        self.assertEqual(record['variables']['recipe'], [{'path': 'steps.review.settings.max_output_tokens',
                                                          'base': 65536, 'value': 131072}])
        self.assertEqual(record['estimate']['new_calls'], {'review': 1, 'duration (unreached)': 1})
        frozen = read(self.experiments/'variant'/'run'/'prompts.json')
        self.assertEqual(frozen['review'], 'PRIMER-COMPATIBLE NEW REVIEW')
        # Batching is a recipe setting; changing it on purpose drops companions.
        folder = self.define('single', {'case': ['c']})
        save(folder/'recipe.json', {'phases': {'finishing': {'batch': {'kind': 'single'}}}})
        record = self.plan('single')
        self.assertEqual(record['cases']['companions'], [])
        self.assertEqual(record['batching']['status'], 'changed')
        # A new component file is composed in and frozen with the run.
        folder = self.define('component', {'case': ['a']})
        (folder/'components').mkdir()
        (folder/'components'/'careful.py').write_text(
            'from pipeline.components.review import Review\nfrom pipeline.components import register\n\n'
            '@register\nclass CarefulReview(Review):\n    name = "careful-review"\n'
            '    summary = "Reviews one case at a time."\n')
        save(folder/'recipe.json', {'steps': {'review': {'component': 'careful-review'}}})
        record = self.plan('component')
        self.assertEqual(record['variables']['components'], ['careful.py'])
        self.assertTrue((self.experiments/'component'/'run'/'components'/'careful.py').exists())

    def test_invalid_definitions_are_refused_before_anything_is_written(self):
        folder = self.define('split', {'case': ['a']})
        save(folder/'recipe.json', {'providers': {'second': {'model': 'deepseek-flash', 'env': 'OTHER'}},
                                    'steps': {'duration': {'provider': 'second'}}})
        with self.assertRaisesRegex(ValueError, 'same provider'):
            self.plan('split')
        folder = self.define('unpriced', {'case': ['a']})
        save(folder/'recipe.json', {'providers': {'default': {'model': 'unpriced-model'}}})
        with self.assertRaisesRegex(ValueError, 'models.json'):
            self.plan('unpriced')
        folder = self.define('moved', {'case': ['c']})
        cohort = read(folder/'cohort.json')
        cohort['cases'][0]['source_sha256'] = '0'*64
        save(folder/'cohort.json', cohort)
        with self.assertRaisesRegex(ValueError, 'different source'):
            self.plan('moved')
        self.define('solo', {'case': ['c']}, companions=False)
        with self.assertRaisesRegex(ValueError, 'batch membership'):
            self.plan('solo')
        self.assertFalse(any(self.experiments.iterdir()))

    def test_cap_covers_new_spending_and_blocks_before_sending(self):
        self.define('capped', {'status': ['review_failed']})
        self.plan('capped')
        self.execute('capped', 1e-9)
        self.assertEqual(self.posts, 0)
        executions = read(self.experiments/'capped'/'executions.json')
        self.assertEqual(executions[0]['new_spend_usd'], 0)
        calls = self.experiments/'capped'/'run'/'calls'
        inherited = [read(p) for p in calls.glob('*.result.json')]
        self.assertTrue(inherited and all(s['inherited_from'] for s in inherited))
        self.assertFalse(list(calls.glob('review-*')))

    def test_run_inherits_identical_work_and_finalize_separates_costs(self):
        self.define('pilot', {'status': ['review_failed']})
        self.plan('pilot')
        self.execute('pilot', 1)
        self.assertEqual(self.posts, 2)  # One review and one duration; triage and extraction inherited.
        self.execute('pilot', 1)
        self.assertEqual(self.posts, 0)  # Resuming reuses completed work.
        comparison = appeal_runner.finalize(SimpleNamespace(root=self.experiments, name='pilot'))
        self.assertEqual(comparison['transitions'], {'review_failed -> complete': 2})
        self.assertEqual(comparison['batching']['observed'], 'preserved')
        self.assertGreater(comparison['cost']['inherited_usd'], 0)
        self.assertGreater(comparison['cost']['new_usd'], 0)
        candidate = read_experiment(self.experiments/'pilot'/'candidate')
        self.assertEqual(candidate['manifest']['execution']['new_model_calls'], 2)
        steps = [s['id'] for phase in candidate['canonical']['recipe']['phases'] for s in phase['steps']]
        self.assertEqual(steps, ['triage', 'extract', 'review', 'duration'])
        with self.assertRaisesRegex(ValueError, 'never replaced'):
            appeal_runner.finalize(SimpleNamespace(root=self.experiments, name='pilot'))
        with self.assertRaisesRegex(ValueError, 'finalized'):
            self.execute('pilot', 1)

    def test_rerun_calls_identical_steps_afresh(self):
        self.define('variance', {'case': ['a', 'b']}, rerun=['review', 'duration'])
        record = self.plan('variance')
        self.assertEqual(record['estimate']['new_calls'], {'review': 1, 'duration (unreached)': 1})
        self.execute('variance', 1)
        self.assertEqual(self.posts, 2)

    def test_retrospective_compare_and_dashboard_views(self):
        from appeal_dashboard import Dashboard
        import shutil
        candidate = self.runs/'candidate-run'
        shutil.copytree(self.base, candidate)
        result = read(candidate/'results'/'c.json')
        bound = {'docket': 'A', 'duration': {'kind': 'at_least', 'days': 5}}
        save(candidate/'results'/'c.json', {**result, 'status': 'complete', 'duration_bounds': [bound]})
        comparison = appeal_runner.retrospective(SimpleNamespace(
            root=self.experiments, name='history', base_run=self.base, candidate_run=candidate, cohort=None,
            hypothesis='Later run recovers c'))
        self.assertEqual(comparison['impact']['selected']['flows'], {'ok': 2, 'recovered': 1, 'still_failing': 1})
        self.define('planned', {'case': ['a']})
        self.plan('planned')
        dashboard = Dashboard(experiments=self.experiments)
        cards = {d['id']: d for d in dashboard.catalog()['datasets']}
        self.assertEqual(cards['experiment:history']['status'], 'finalized')
        self.assertEqual(cards['experiment:history']['variables'], ['Same prompts and settings'])
        self.assertEqual(cards['experiment:planned']['status'], 'planned')
        self.assertEqual(dashboard.overview('experiment:planned')['progress']['cases'], 2)
        self.assertEqual([r['case_id'] for r in dashboard.cases('experiment:history', verdict='improved')['cases']], ['c'])
        self.assertEqual(dashboard.detail('experiment:history', 'c')['impact']['processing']['flow'], 'recovered')
        for bad in ('experiment:../base', 'experiment:missing', 'experiment:'):
            with self.assertRaises(ValueError):
                dashboard.overview(bad)
        with self.assertRaises(ValueError):
            dashboard.cases('experiment:planned')


if __name__ == '__main__':
    unittest.main()
