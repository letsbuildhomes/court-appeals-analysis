"""The lab: comparing results maps, reference scoring, experiment rounds and adoption."""
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from court.config import ROOT
from court.contracts import results
from court.core.context import Cache
from court.core.datasets import Datasets, Failure
from court.core.library import Library
from court.core.models import CallStore
from court.core.pipelines import Pipelines
from court.core.runner import Runner
from court.lab import compare
from court.lab.experiments import ExperimentError, Lab, Regressions

U = results.UNKNOWN
JUDGE = '''
from court.contracts import results
from court.core.step import Step


class Judge(Step):
    inputs = {'cases': None}
    output = 'results'
    defaults = {'longer_than': 3, 'batch_cases': 1}

    def run(self, inputs, ctx):
        out = {}
        for key, word in inputs['cases'].items():
            value = 'in_scope' if len(word) > self.settings['longer_than'] else 'excluded'
            out[key] = results.result(key, {'value': value, 'reason': 'length', 'municipal_appeal': 'unknown',
                                            'housing': 'unknown', 'evidence': []})
        return out
'''

PREP = '''
from court.core.step import Step


class Prep(Step):
    inputs = {'words': None}
    defaults = {'extend': [], 'drop': []}

    def run(self, inputs, ctx):
        return {k: w + '!!' if k in self.settings['extend'] else w
                for k, w in inputs['words'].items() if k not in self.settings['drop']}
'''


def disposition(value='in_scope'):
    return {'value': value, 'reason': 'r', 'municipal_appeal': 'yes', 'housing': 'yes', 'evidence': []}


def record(docket='A', outcome=U, days=U):
    duration = {'start': U, 'end': U, 'note': 'n', 'kind': U, 'days': U}
    if days != U:
        duration = {'start': {'date': '2020-01-01', 'relation': 'exact', 'description': 'd', 'evidence': []},
                    'end': {'date': '2020-01-11', 'relation': 'exact', 'description': 'd', 'evidence': []},
                    'note': 'n', 'kind': 'exact', 'days': 10}
    return {'docket': docket,
            'project': {'description': U, 'municipality': U, 'proposed_units': U, 'proposed_lots': U, 'evidence': []},
            'municipal_decision': {'description': U, 'date': U, 'outcome': outcome, 'evidence': []},
            'appellants': [], 'discretion': {'description': U, 'involved': U, 'evidence': [], 'central': U,
                                             'central_evidence': []},
            'observed_status': {'litigation': U, 'permission': U, 'description': U, 'evidence': []},
            'timeline': [], 'duration': duration, 'restrictions': [], 'housing_changes': [], 'related_dockets': []}


class CompareTests(unittest.TestCase):
    def test_verdicts_flows_and_reference_scoring(self):
        baseline = {'a': results.result('a', disposition(), [record()]),
                    'b': Failure('delivery', 'timed out'),
                    'c': results.result('c', disposition(), [record(outcome='denied')]),
                    'd': results.result('d', disposition(), [record(outcome='denied')])}
        candidate = {'a': results.result('a', disposition(), [record(days=10)]),
                     'b': results.result('b', disposition(), [record()]),
                     'c': results.result('c', disposition(), [record(outcome='approved')]),
                     'd': Failure('validation', 'bad answer')}
        reference = {'c': {'result': results.result('c', disposition(), [record(outcome='approved')]),
                           'labeled': {'case': [], 'records': {'A': ['decision_outcome']}}}}
        comparison = compare.compare(baseline, candidate, reference)
        verdicts = {c['case_id']: (c['verdict'], c['processing']['flow']) for c in comparison['cases']}
        self.assertEqual(verdicts, {'a': ('improved', 'ok'), 'b': ('improved', 'recovered'),
                                    'c': ('improved', 'ok'), 'd': ('worsened', 'broke')})
        self.assertEqual(comparison['accuracy']['corrected'], 1)
        self.assertEqual(compare.regressions(comparison), ['d'])
        self.assertIn('1 of 4 recovered', comparison['summary']['headline'])

    def test_a_settled_baselines_unsent_calls_are_failures(self):
        baseline = {'a': Failure('not_sent', 'not in the call cache')}
        candidate = {'a': results.result('a', disposition(), [record()])}
        open_run = compare.compare(baseline, candidate)
        self.assertEqual(open_run['cases'][0]['processing']['flow'], 'missing')
        self.assertEqual(compare.unfinished(open_run), ['a'])
        settled = compare.compare(baseline, candidate, settled=True)
        self.assertEqual(settled['cases'][0]['processing']['flow'], 'recovered')
        self.assertEqual(compare.unfinished(settled), [])

    def test_records_are_matched_by_docket_across_cases(self):
        # The baseline held docket B twice (in x and y); the candidate split x, moving B to its own case.
        baseline = {'x': results.result('x', disposition(), [record('A'), record('B')]),
                    'y': results.result('y', disposition(), [record('B')])}
        candidate = {'x': results.result('x', disposition(), [record('A')]),
                     'g-b': results.result('g-b', disposition(), [record('B', days=10)])}
        comparison = compare.compare(baseline, candidate, inputs={'x': 'changed', 'y': 'removed', 'g-b': 'added'})
        summary = comparison['summary']
        self.assertEqual((summary['baseline']['dockets'], summary['baseline']['duplicate_records']), (2, 1))
        self.assertEqual((summary['candidate']['dockets'], summary['candidate']['duplicate_records']), (2, 0))
        self.assertEqual(summary['records']['lost'], 0)
        self.assertEqual(summary['records']['duplicates_removed'], 1)
        cases = {c['case_id']: c for c in comparison['cases']}
        self.assertEqual(cases['x']['records']['moved_out'], ['B'])
        self.assertEqual(cases['g-b']['records']['moved_in'], ['B'])
        self.assertEqual(cases['g-b']['records']['matched'], 1)
        # Completeness counts the records both sides have, never a missing side.
        self.assertEqual(summary['baseline']['matched_slots'], summary['candidate']['matched_slots'])

    def test_failures_dont_raise_completeness_and_chance_isnt_a_regression(self):
        baseline = {'a': results.result('a', disposition(), [record()]),
                    'b': results.result('b', disposition(), [record('B', outcome='denied')])}
        candidate = {'a': Failure('delivery', 'output limit'),
                     'b': results.result('b', disposition(), [record('B', outcome='other')])}
        comparison = compare.compare(baseline, candidate, inputs={'a': 'same_documents', 'b': 'same_documents'})
        summary = comparison['summary']
        self.assertEqual(summary['records']['lost_by_reason'], {'failed': 1})
        self.assertEqual(summary['baseline']['matched_slots'], summary['candidate']['matched_slots'])
        self.assertIn('answer changes are chance', ' '.join(comparison['cases'][1]['chips']))
        # A case that broke still counts; a finished one with the same documents changed only by chance.
        self.assertEqual(compare.regressions(comparison), ['a'])

    def test_only_listed_facts_are_scored(self):
        truth = results.result('c', disposition(), [record(outcome='approved')])
        baseline = {'c': results.result('c', disposition(), [record(outcome='denied')])}
        candidate = {'c': results.result('c', disposition(), [record(outcome='approved')])}

        def scored(labeled):
            item = {'result': truth} if labeled is None else {'result': truth, 'labeled': labeled}
            accuracy = compare.compare(baseline, candidate, {'c': item})['accuracy']
            return accuracy and accuracy['corrected']
        self.assertIsNone(scored(None))
        self.assertEqual(scored({'records': 'all'}), 1)
        self.assertEqual(scored({'records': {'A': 'all'}}), 1)
        self.assertIsNone(scored({'records': {'B': 'all'}}))


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root/'steps/toy').mkdir(parents=True)
        (root/'steps/toy/judge.py').write_text(JUDGE)
        (root/'steps/toy/prep.py').write_text(PREP)
        (root/'steps/flow').mkdir(parents=True)
        shutil.copy(ROOT/'steps/flow/sequence.py', root/'steps/flow/sequence.py')
        (root/'prompts').mkdir()
        self.pipelines = Pipelines(root/'pipelines', Library(root))
        self.datasets = Datasets(root/'datasets')
        runner = Runner(self.pipelines, self.datasets, root/'runs', CallStore(root/'calls'), Cache(root/'cache'))
        self.lab = Lab(root/'experiments', self.pipelines, self.datasets, runner)
        base = self.pipelines.resolve({'name': 'base', 'root': {'name': 'judge', 'step': 'toy:judge',
                                                                'save': 'results'}})
        self.pipelines.set_tag('production', base['id'])
        self.words = self.datasets.write('words', {'a': 'hi', 'b': 'hello', 'c': 'hey', 'd': 'howdy'})
        self.full = runner.run('full', base['id'], {'cases': self.words.id}, save_all=True)
        self.runner = runner

    def two_steps(self):
        """A pipeline whose first step prepares the words the second judges, run once over everything."""
        tree = self.pipelines.resolve({'name': 'two', 'root': {
            'name': 'two', 'step': 'flow:sequence', 'settings': {'inputs': ['words']}, 'children': [
                {'name': 'prep', 'step': 'toy:prep', 'save': 'output', 'inputs': {'words': 'input:words'}},
                {'name': 'judge', 'step': 'toy:judge', 'save': 'results', 'inputs': {'cases': 'prep'}}]}})
        self.pipelines.set_tag('production', tree['id'])
        self.runner.run('two-full', tree['id'], {'words': self.words.id}, save_all=True)
        return tree

    def tearDown(self):
        self.tmp.cleanup()

    def test_rounds_compare_with_the_existing_baseline_and_adoption_needs_a_clean_confirm(self):
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'],
                     confirm=['a', 'b', 'c', 'd'], changes=[{'at': '', 'settings': {'longer_than': 2}}])
        plan = self.lab.plan('looser', 'probe')
        self.assertEqual(plan['changes'][0]['settings'], [None, {'longer_than': 2}])
        self.assertEqual(self.lab.plans('looser')['probe']['keys'], 1)
        probe = self.lab.run('looser', 'probe', mode='cached')
        self.assertEqual(probe['baseline']['run'], 'full')
        # The baseline side is read from its complete run; only the candidate's round is a run, and no plan.
        self.assertEqual(sorted(r['name'] for r in self.runner.all()), ['full', probe['candidate']['run']])
        self.assertEqual(probe['summary']['verdicts'], {'unchanged': 1})
        with self.assertRaises(ExperimentError):
            self.lab.adopt('looser')
        confirm = self.lab.run('looser', 'confirm', mode='cached')
        self.assertEqual(confirm['summary']['verdicts'], {'changed': 1, 'unchanged': 3})
        self.assertEqual(confirm['steps']['(root)']['changed'], 1)
        entry = self.lab.adopt('looser')
        self.assertEqual(self.pipelines.load('production')['id'], confirm['candidate']['pipeline'])
        self.assertEqual(entry['evidence']['experiment'], 'looser')

    def test_adopting_cases_that_got_worse_needs_acceptance(self):
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'],
                     confirm=['a', 'b', 'c', 'd'], changes=[{'at': '', 'settings': {'longer_than': 2}}])
        self.lab.run('looser', 'confirm', mode='cached')
        path = self.lab.folder('looser')/'rounds/confirm.json'
        round_ = json.loads(path.read_text())
        path.write_text(json.dumps(round_ | {'regressions': ['c']}))
        with self.assertRaises(Regressions) as raised:
            self.lab.adopt('looser')
        self.assertEqual([c['case_id'] for c in raised.exception.cases], ['c'])
        entry = self.lab.adopt('looser', accept_regressions=True)
        self.assertEqual(entry['evidence']['regressions'], ['c'])

    def test_a_focus_runs_earlier_steps_on_everything_and_chooses_changed_cases(self):
        self.two_steps()
        self.lab.new('longer', 'Longer words count', inputs={'words': self.words.id}, focus='judge', probe='changed',
                     confirm='all', changes=[{'at': 'prep', 'settings': {'extend': ['a'], 'drop': ['d']}}])
        probe = self.lab.run('longer', 'probe', mode='cached')
        self.assertEqual(probe['keys'], ['a', 'd'])
        self.assertEqual(probe['inputs'], {'a': 'changed', 'd': 'removed'})
        self.assertEqual(probe['steps']['prep']['removed'], 1)
        verdicts = {c['case_id']: c['verdict'] for c in probe['cases']}
        self.assertEqual(verdicts, {'a': 'changed', 'd': 'unchanged'})
        merged = compare.compare_case('m', results.result('m', disposition(), [record()]), None, input_change='removed')
        self.assertEqual(merged['verdict'], 'changed')
        self.assertIn('merged into another case or removed', ' '.join(probe['cases'][1]['chips']))
        confirm = self.lab.run('longer', 'confirm', mode='cached')
        self.assertEqual(confirm['keys'], ['a', 'b', 'c', 'd'])
        self.assertEqual(confirm['summary']['inputs'], {'changed': 1, 'removed': 1, 'same': 2})
        self.lab.adopt('longer')

    def test_a_baseline_that_batches_needs_its_complete_run(self):
        batched = self.pipelines.resolve({'name': 'batched', 'root': {'name': 'judge', 'step': 'toy:judge',
                                          'save': 'results', 'settings': {'batch_cases': 2}}})
        self.pipelines.set_tag('production', batched['id'])
        self.runner.run('partial', batched['id'], {'cases': self.words.id}, keys=['a'], save_all=True)
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'],
                     changes=[{'at': '', 'settings': {'longer_than': 2}}])
        with self.assertRaisesRegex(ExperimentError, 'no complete run'):
            self.lab.run('looser', 'probe', mode='cached')
        with self.assertRaisesRegex(ExperimentError, 'batches cases'):
            self.lab.baseline('looser', keys=['a'])

    def test_a_baseline_that_doesnt_batch_can_run_over_just_the_rounds_cases(self):
        (self.runner.root/'full/run.json').unlink()
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'],
                     confirm=['a', 'b'], changes=[{'at': '', 'settings': {'longer_than': 2}}])
        with self.assertRaisesRegex(ExperimentError, 'lab baseline looser'):
            self.lab.run('looser', 'probe', mode='cached')
        made = self.lab.baseline('looser', keys=['a', 'b'])
        probe = self.lab.run('looser', 'probe', mode='cached')
        self.assertEqual(probe['baseline']['run'], made['name'])
        self.assertEqual(self.lab.run('looser', 'confirm', mode='cached')['summary']['cases'], 2)

    def test_a_focus_limits_only_the_focused_step(self):
        tree = self.two_steps()
        record = self.runner.run('focused', tree['id'], {'words': self.words.id}, focus='judge', keys=['b', 'x'])
        self.assertEqual(record['focused'], {'keys': ['b'], 'removed': []})
        self.assertEqual(len(self.datasets.open(record['outputs']['prep'])), 4)
        # Saving every step, the sequence's output is its last step's results, not a generic copy.
        full = self.runner.load('two-full')
        self.assertEqual(full['output'], full['outputs']['judge'])
        self.assertEqual(self.datasets.open(full['output']).kind, 'results')
        self.assertEqual(self.datasets.open(record['output']).keys(), ['b'])


if __name__ == '__main__':
    unittest.main()
