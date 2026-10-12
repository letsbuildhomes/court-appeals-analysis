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
from court.lab.experiments import ExperimentError, Lab

U = results.UNKNOWN
JUDGE = '''
from court.contracts import results
from court.core.step import Step


class Judge(Step):
    inputs = {'cases': None}
    output = 'results'
    defaults = {'longer_than': 3}

    def run(self, inputs, ctx):
        out = {}
        for key, word in inputs['cases'].items():
            value = 'in_scope' if len(word) > self.settings['longer_than'] else 'excluded'
            out[key] = results.result(key, {'value': value, 'reason': 'length', 'municipal_appeal': 'unknown',
                                            'housing': 'unknown', 'evidence': []})
        return out
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


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root/'steps/toy').mkdir(parents=True)
        (root/'steps/toy/judge.py').write_text(JUDGE)
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
        self.full = runner.run('full', base['id'], {'cases': self.words.id})

    def tearDown(self):
        self.tmp.cleanup()

    def test_rounds_compare_with_the_existing_baseline_and_adoption_needs_a_clean_confirm(self):
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'],
                     confirm=['a', 'b', 'c', 'd'], changes=[{'at': '', 'settings': {'longer_than': 2}}])
        plan = self.lab.plan('looser', 'probe')
        self.assertEqual(plan['changes'][0]['settings'], [None, {'longer_than': 2}])
        probe = self.lab.run('looser', 'probe', mode='cached')
        self.assertEqual(probe['baseline']['run'], 'full')
        self.assertEqual(probe['summary']['verdicts'], {'unchanged': 1})
        with self.assertRaises(ExperimentError):
            self.lab.adopt('looser')
        confirm = self.lab.run('looser', 'confirm', mode='cached')
        self.assertEqual(confirm['summary']['verdicts'], {'changed': 1, 'unchanged': 3})
        self.assertEqual(confirm['steps']['(root)']['changed'], 1)
        entry = self.lab.adopt('looser')
        self.assertEqual(self.pipelines.load('production')['id'], confirm['candidate']['pipeline'])
        self.assertEqual(entry['evidence']['experiment'], 'looser')


if __name__ == '__main__':
    unittest.main()
