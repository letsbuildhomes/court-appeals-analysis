"""Experiment cohorts, planning, capped execution and finalization; no network."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import appeal_runner
from appeal_cohorts import create_cohort, read_cohort, select_cases
from appeal_experiments import create_experiment, read_experiment
from appeal_runtime import digest, read, save

PROMPTS = {k: k.upper() for k in ('system', 'triage', 'extract', 'primer', 'review', 'duration')}
SETTINGS = {'model': 'model', 'reasoning': 'high', 'max_output_tokens': 65536}
TRIAGE = {'schema_version': 'v5-triage', 'municipal_appeal': 'yes', 'housing': 'yes'}


def candidate():
    return {'schema_version': 'v6', 'dockets': [{
        'docket': 'A', 'related_dockets': [],
        'project': {'description': None, 'municipality': None, 'proposed_units': None, 'proposed_lots': None, 'evidence': []},
        'municipal_decision': {'description': None, 'date': None, 'outcome': 'unclear', 'evidence': []},
        'appellants': [],
        'discretion': {'description': None, 'evidence': [], 'involved': 'unclear', 'central': 'unclear', 'central_evidence': []},
        'timeline': [], 'observed_status': {'litigation': 'unclear', 'permission': 'unclear', 'description': None, 'evidence': []},
        'restrictions': [], 'housing_changes': []}]}


def reply(*args, **kwargs):
    request = kwargs['json']
    cases = json.loads(request['messages'][-1]['content'])['cases']
    if len(request['messages']) == 2:
        body = {'reviews': [{'group': c['group'], 'review': {'schema_version': 'v6-review-1',
                 'scope': {'verdict': 'agree'}, 'changes': [], 'flags': []}} for c in cases]}
    else:
        body = {'groups': [{'group': c['group'], 'dockets': [{'docket': 'A', 'start': None, 'end': None,
                 'note': 'No supported bounds'}]} for c in cases]}
    response = Mock(status_code=200, text=json.dumps(body))
    response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}],
                                  'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}
    return response


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root/'base'
        self.build_base()
        self.artifact = self.root/'artifact'
        create_experiment(self.base, self.artifact)
        self.experiments = self.root/'experiments'

    def build_base(self):
        """Batches (a, b) completed and (c, d) failed review."""
        groups = []
        save(self.base/'prompts.json', PROMPTS)
        for gid in 'abcd':
            source = f'<page number="1">Source {gid}</page>'
            (self.base/'sources').mkdir(parents=True, exist_ok=True)
            (self.base/'sources'/f'{gid}.txt').write_text(source)
            groups.append({'group_id': gid, 'source_sha256': digest(source), 'eligible': True,
                           'documents': [], 'dockets': []})
            request = appeal_runner.triage_request(SETTINGS, PROMPTS, source)
            save(self.base/f'calls/{gid}.triage.request.json', request)
            save(self.base/f'calls/{gid}.triage.result.json', {'status': 'ok', 'charged_or_reserved_usd': 0.01})
            save(self.base/f'calls/{gid}.triage.parsed.json', TRIAGE)
            draft = {'group': gid, 'status': 'extracted', 'warnings': [], 'triage': TRIAGE,
                     'route': {'route': 'extract'}, 'candidate': candidate()}
            save(self.base/f'first_pass/{gid}.json', draft)
            if gid in 'ab':
                bound = {'docket': 'A', 'duration': {'kind': 'unknown', 'days': None}}
                save(self.base/f'results/{gid}.json', {**draft, 'status': 'complete', 'duration_bounds': [bound]})
            else:
                save(self.base/f'results/{gid}.json', {**draft, 'status': 'review_failed', 'error': 'Output limit'})
        for members, status in (('ab', 'ok'), ('cd', 'failed')):
            name = 'review-'+digest('|'.join(members))[:16]
            data = {'cases': [{'group': g} for g in members]}
            save(self.base/f'calls/{name}.request.json', {'messages': [{'role': 'system', 'content': 'x'},
                                                                     {'role': 'user', 'content': json.dumps(data)}]})
            save(self.base/f'calls/{name}.result.json', {'status': status, 'charged_or_reserved_usd': 0.1})
        save(self.base/'manifest.json', {'schema_version': 'appeals-1', 'name': 'base', 'created_at': '2026-01-01T00:00:00+00:00',
             **SETTINGS, 'max_chars': 600000, 'review_batch_size': 2, 'review_batch_chars': 200000,
             'prompt_sha256': digest(json.dumps(PROMPTS, sort_keys=True)), 'groups': groups})
        (self.base/'.lock').touch()

    def cohort(self, query, name='cohort.json'):
        create_cohort(self.artifact, self.root/name, query, name='test', description='Test cohort')
        return self.root/name

    def plan(self, name, cohort, **options):
        values = dict(root=self.experiments, name=name, base_run=self.base, cohort=cohort, hypothesis='Test',
                      prompt=None, model=None, reasoning=None, max_output_tokens=None, review_max_output_tokens=None,
                      rerun_from=None, no_companions=False, allow_batch_change=False, price_multiplier=1)
        values.update(options)
        return appeal_runner.plan(SimpleNamespace(**values))

    def execute(self, name, max_spend):
        args = SimpleNamespace(root=self.experiments, name=name, max_spend=max_spend, price_multiplier=1, workers=1,
                               first_pass_only=False, api_base_url=None, api_url=None, retry_failed=False,
                               retry_max_output_tokens=None)
        with patch('appeal_runtime.httpx.Client') as transport, \
             patch.dict('os.environ', {'APPEALS_API_BASE_URL': 'http://127.0.0.1:9', 'APPEALS_API_KEY': 'fake'}):
            post = transport.return_value.__enter__.return_value.post
            post.side_effect = reply
            appeal_runner.execute(args)
        return post.call_count

    def test_cohort_saves_query_identities_and_hashes(self):
        cohort = read_cohort(self.cohort({'status': ['review_failed']}))
        self.assertEqual([c['case_id'] for c in cohort['cases']], ['c', 'd'])
        self.assertEqual(cohort['query'], {'status': ['review_failed']})
        self.assertEqual(cohort['cases'][0]['source_sha256'], digest('<page number="1">Source c</page>'))
        with self.assertRaises(FileExistsError):
            self.cohort({'status': ['review_failed']})
        cases = read_experiment(self.artifact)['cases']
        self.assertEqual(select_cases(cases, {'duration_kind': ['unknown']}), ['a', 'b'])
        sample = {'scope': ['confirmed'], 'sample': 2, 'seed': 7}
        self.assertEqual(select_cases(cases, sample), select_cases(cases, sample))
        self.assertEqual(len(select_cases(cases, sample)), 2)
        for query in ({}, {'sample': 2}, {'status': 'review_failed'}, {'unknown': ['x']}, {'case': ['absent']}):
            with self.assertRaises(ValueError):
                select_cases(cases, query)

    def test_plan_adds_companions_reuses_identical_work_and_makes_no_calls(self):
        with patch('appeal_runtime.httpx.Client') as transport:
            record = self.plan('replay', self.cohort({'case': ['c']}))
            transport.assert_not_called()
        self.assertEqual(record['cases'], {'selected': ['c'], 'companions': ['d']})
        self.assertEqual(record['batching']['status'], 'preserved')
        self.assertEqual(sorted(record['reuse']['calls']), ['c.triage', 'd.triage'])  # Failed review not reused.
        self.assertAlmostEqual(record['reuse']['inherited_usd'], 0.02)
        self.assertEqual(record['estimate']['new_calls'], {'review': 1, 'duration': 1})
        self.assertAlmostEqual(record['estimate']['historical_usd'], 0.1)
        run = self.experiments/'replay'/'run'
        self.assertEqual(sorted(p.name for p in (run/'first_pass').iterdir()), ['c.json', 'd.json'])
        self.assertEqual([g['group_id'] for g in read(run/'manifest.json')['groups']], ['c', 'd'])
        self.assertNotIn('review_max_output_tokens', read(run/'manifest.json'))
        with self.assertRaises(ValueError):
            self.plan('replay', self.cohort({'case': ['a']}, 'other.json'))  # Never replaced.

    def test_changed_variables_invalidate_dependent_stages_only(self):
        prompt = self.root/'review.md'
        prompt.write_text('NEW REVIEW')
        record = self.plan('prompt', self.cohort({'case': ['a', 'b']}), prompt=[f'review={prompt}'])
        self.assertEqual(record['reuse']['stages'], {'triage': True, 'extract': True, 'review': False, 'duration': False})
        self.assertNotIn('review-'+digest('a|b')[:16], record['reuse']['calls'])
        self.assertEqual(read(self.experiments/'prompt'/'run'/'prompts.json')['review'], 'NEW REVIEW')
        record = self.plan('tokens', self.root/'cohort.json', review_max_output_tokens=131072)
        self.assertEqual(record['reuse']['stages']['extract'], True)
        self.assertEqual(read(self.experiments/'tokens'/'run'/'manifest.json')['review_max_output_tokens'], 131072)
        record = self.plan('model', self.root/'cohort.json', model='other')
        self.assertEqual(record['reuse']['calls'], [])
        record = self.plan('rerun', self.root/'cohort.json', rerun_from='review')
        self.assertEqual(record['reuse']['stages'], {'triage': True, 'extract': True, 'review': False, 'duration': False})

    def test_plan_refuses_changed_sources_and_unrecorded_batching(self):
        cohort = self.cohort({'case': ['c']})
        with self.assertRaisesRegex(ValueError, 'batch'):
            self.plan('solo', cohort, no_companions=True)
        record = self.plan('solo', cohort, no_companions=True, allow_batch_change=True)
        self.assertEqual(record['variables']['batching'], 'changed')
        value = json.loads(cohort.read_text())
        value['cases'][0]['source_sha256'] = '0'*64
        cohort.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'different source'):
            self.plan('changed', cohort)
        self.assertFalse((self.experiments/'changed').exists())

    def test_cap_covers_new_spending_and_blocks_before_sending(self):
        self.plan('capped', self.cohort({'status': ['review_failed']}))
        self.assertEqual(self.execute('capped', 1e-9), 0)
        executions = read(self.experiments/'capped'/'executions.json')
        self.assertEqual(executions[0]['new_spend_usd'], 0)
        self.assertEqual(executions[0]['status_counts'], {'review_failed': 2})
        self.assertFalse(list((self.experiments/'capped'/'run'/'calls').glob('review-*')))

    def test_run_finalize_compares_cohort_and_separates_costs(self):
        self.plan('pilot', self.cohort({'status': ['review_failed']}))
        self.assertEqual(self.execute('pilot', 1), 2)  # One review and one duration call.
        self.assertEqual(self.execute('pilot', 1), 0)  # Resuming reuses completed work.
        comparison = appeal_runner.finalize(SimpleNamespace(root=self.experiments, name='pilot'))
        self.assertEqual(comparison['transitions'], {'review_failed -> complete': 2})
        self.assertEqual(comparison['records'], {'baseline': 0, 'candidate': 2})
        self.assertEqual(comparison['batching']['observed'], 'preserved')
        self.assertAlmostEqual(comparison['cost']['inherited_usd'], 0.02)
        self.assertGreater(comparison['cost']['new_usd'], 0)
        baseline = read_experiment(self.experiments/'pilot'/'baseline')
        candidate = read_experiment(self.experiments/'pilot'/'candidate')
        self.assertEqual([c['case_id'] for c in baseline['cases']], ['c', 'd'])
        self.assertAlmostEqual(baseline['canonical']['provenance']['cost']['recorded_usd'], 0.12)
        self.assertEqual(candidate['manifest']['kind'], 'experiment_candidate')
        self.assertEqual(candidate['manifest']['execution']['new_model_calls'], 2)
        with self.assertRaisesRegex(ValueError, 'never replaced'):
            appeal_runner.finalize(SimpleNamespace(root=self.experiments, name='pilot'))
        with self.assertRaisesRegex(ValueError, 'finalized'):
            self.execute('pilot', 1)


    def test_retrospective_compare_and_dashboard_views(self):
        from appeal_dashboard import Dashboard
        import shutil
        candidate = self.root/'candidate-run'
        shutil.copytree(self.base, candidate)
        result = read(candidate/'results'/'c.json')
        bound = {'docket': 'A', 'duration': {'kind': 'at_least', 'days': 5}}
        save(candidate/'results'/'c.json', {**result, 'status': 'complete', 'duration_bounds': [bound]})
        comparison = appeal_runner.retrospective(SimpleNamespace(
            root=self.experiments, name='history', base_run=self.base, candidate_run=candidate, cohort=None,
            hypothesis='Later run recovers c'))
        self.assertEqual(comparison['impact']['selected']['flows'], {'ok': 2, 'recovered': 1, 'still_failing': 1})
        self.assertEqual(read(self.experiments/'history'/'experiment.json')['kind'], 'retrospective')
        self.plan('planned', self.cohort({'case': ['a']}))
        dashboard = Dashboard(experiments=self.experiments)
        cards = {d['id']: d for d in dashboard.catalog()['datasets']}
        self.assertEqual(cards['experiment:history']['status'], 'finalized')
        self.assertEqual(cards['experiment:history']['variables'], ['Same prompts and settings'])
        self.assertEqual(cards['experiment:planned']['status'], 'planned')
        self.assertEqual(dashboard.overview('experiment:planned')['progress']['cases'], 2)
        rows = dashboard.cases('experiment:history', verdict='improved')['cases']
        self.assertEqual([r['case_id'] for r in rows], ['c'])
        self.assertEqual(dashboard.cases('experiment:history')['cases'][0]['impact']['verdict'], 'improved')
        detail = dashboard.detail('experiment:history', 'c')
        self.assertEqual(detail['impact']['processing']['flow'], 'recovered')
        for bad in ('experiment:../base', 'experiment:missing', 'experiment:'):
            with self.assertRaises(ValueError):
                dashboard.overview(bad)
        with self.assertRaises(ValueError):
            dashboard.cases('experiment:history', verdict='better')
        with self.assertRaises(ValueError):
            dashboard.cases('experiment:planned')


if __name__ == '__main__':
    unittest.main()
