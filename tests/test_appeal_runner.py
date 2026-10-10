"""Experiments as modules: init, cohort, plan, run, finalize, compare and tag; fake model, no network."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import appeal_runner
from appeal_cohorts import create_cohort
from appeal_runtime import read, save
from pipeline import corpus, registry, runs
from pipeline.providers import ChatCompletionsProvider
from pipeline.store import CallStore
from tests.test_pipeline import PROMPTS, FakeModel, passthrough_extract, source

IDS = ['good-a', 'good-b', 'good-c', 'good-d']
STRICT = '''from pipeline.steps import Prompt


def build(base):
    return base.update('finishing/review', prompts={'review': Prompt('REVIEW STRICTLY')})
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        folder = self.root/'corpus'
        groups = []
        for case_id in IDS:
            text = source(case_id)
            save(folder/'sources'/f'{case_id}.txt', None)
            (folder/'sources'/f'{case_id}.txt').write_text(text)
            groups.append({'group_id': case_id, 'dockets': ['A'], 'documents': [],
                           'source_sha256': hashlib.sha256(text.encode()).hexdigest()})
        save(folder/'manifest.json', {'schema_version': 'appeals-1', 'groups': groups})
        for target, value in ((corpus, {'PRODUCTION': folder}),
                              (registry, {'PIPELINES': self.root/'pipelines', 'TAGS': self.root/'tags.jsonl',
                                          'EXPERIMENTS': self.root/'definitions'}),
                              (runs, {'RUNS': self.root/'runs', 'EXPERIMENT_RUNS': self.root/'experiments',
                                      'STORE': self.root/'calls'})):
            for name, path in value.items():
                patcher = patch.object(target, name, path)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.args = SimpleNamespace(root=self.root/'experiments', definitions=self.root/'definitions',
                                    runs=self.root/'runs', store=self.root/'calls', price_multiplier=1, workers=2,
                                    retry_failed=False, retry_max_output_tokens=None, api_base_url=None, api_url=None)
        environment = patch.dict('os.environ', {'APPEALS_API_BASE_URL': 'http://127.0.0.1:9', 'APPEALS_API_KEY': 'fake'})
        environment.start()
        self.addCleanup(environment.stop)
        self.model = FakeModel()
        self.production()

    def fake(self):
        return patch.object(ChatCompletionsProvider, 'post', side_effect=self.model)

    def production(self):
        """A tagged baseline pipeline and its run over the corpus."""
        source_ref = {'module': 'pipeline.legacy', 'options': {'prompt_overrides': PROMPTS}}
        self.base_id = registry.freeze(registry.build(source_ref), source_ref)
        registry.set_tag('production', self.base_id, 'Ported legacy pipeline')
        runs.create(self.root/'runs/production', name='production', pipeline_id=self.base_id, case_ids=IDS,
                    corpus=corpus.Corpus())
        with self.fake(), passthrough_extract():
            runs.run(self.root/'runs/production', store=CallStore(self.root/'calls'), limit=1, offline=True)
        runs.seal(self.root/'runs/production')

    def define(self, name, code=STRICT, cohort=('good-a',), **options):
        args = SimpleNamespace(**vars(self.args), name=name, hypothesis='A stricter editor changes fewer fields',
                               baseline='production')
        folder = appeal_runner.init(args)
        (folder/'experiment.py').write_text(code)
        create_cohort(self.root/'runs/production', folder/'cohort.json', {'case': list(cohort)}, name=name,
                      description='Test cohort')
        if options:
            definition = read(folder/'experiment.json')
            save(folder/'experiment.json', {**definition, **options})
        return SimpleNamespace(**vars(args), max_spend=1.0)

    def test_plan_run_finalize_and_tag(self):
        args = self.define('strict')
        self.model.requests.clear()
        record = appeal_runner.plan(args)
        self.assertEqual(self.model.requests, [])  # Planning makes no model calls.
        self.assertEqual(record['cases'], {'selected': ['good-a'], 'companions': ['good-b']})
        self.assertEqual(record['baseline']['pipeline'], self.base_id)
        self.assertEqual(record['baseline']['rebuilt'], self.base_id)
        self.assertEqual(record['batching']['status'], 'preserved')
        estimate = record['estimate']
        self.assertEqual(estimate['new_calls'], {'finishing/review': 1, 'finishing/duration (unreached)': 2})
        self.assertGreater(estimate['upper_bound_usd'], 0)
        self.assertEqual(estimate['reused_calls'], 4)  # Both cases' triage and extraction.
        status = appeal_runner.describe(args.root/'strict')
        self.assertEqual(status['changes'], ['finishing/review: review prompt'])
        with self.fake(), passthrough_extract():
            execution = appeal_runner.execute(args)
        self.assertEqual(len(self.model.requests), 2)  # Only the changed review and the duration after it.
        self.assertIn('REVIEW STRICTLY', self.model.requests[0]['messages'][0]['content'])
        self.assertGreater(execution['new_spend_usd'], 0)
        comparison = appeal_runner.finalize(args)
        self.assertEqual(comparison['impact']['selected']['cases'], 1)
        self.assertEqual(comparison['impact']['companion']['cases'], 1)
        self.assertEqual(comparison['batching']['observed'], 'preserved')
        self.assertEqual(comparison['cost']['new_calls'], 2)
        with self.assertRaisesRegex(ValueError, 'sealed'):
            runs.run(args.root/'strict/run', store=CallStore(args.store), limit=1, offline=True,
                     experiments=args.definitions)
        with self.assertRaisesRegex(ValueError, 'Already finalized'):
            appeal_runner.finalize(args)
        candidate = record['candidate']['pipeline']
        entry = appeal_runner.tag(SimpleNamespace(**vars(args), tag='production', pipeline=candidate,
                                                  note='Adopted the strict editor', experiment='strict'))
        self.assertEqual(entry['evidence']['experiment'], 'strict')
        self.assertEqual(registry.resolve('production'), candidate)
        self.assertEqual([e['pipeline'] for e in registry.history('production')], [self.base_id, candidate])
        # An experiment can now build on the new production pipeline, which the strict run covers.
        follow = self.define('stricter', code=STRICT.replace('STRICTLY', 'MORE STRICTLY'))
        self.assertEqual(appeal_runner.plan(follow)['baseline']['run'], str(args.root/'strict/run'))

    def test_changed_experiment_code_refuses_to_run_its_old_plan(self):
        args = self.define('strict')
        appeal_runner.plan(args)
        (args.definitions/'strict/experiment.py').write_text(STRICT.replace('STRICTLY', 'DIFFERENTLY'))
        with self.assertRaisesRegex(ValueError, 'changed since'):
            appeal_runner.execute(args)

    def test_batching_changes_must_be_declared(self):
        args = self.define('alone', companions=False)
        with self.assertRaisesRegex(ValueError, 'Batch membership'):
            appeal_runner.plan(args)
        args = self.define('alone-declared', companions=False, allow_batch_change=True)
        self.assertEqual(appeal_runner.plan(args)['batching']['status'], 'changed')

    def test_noise_floor_samples_rerun_chosen_steps(self):
        args = self.define('noise', code='def build(base):\n    return base\n', samples={'finishing/review': 1})
        record = appeal_runner.plan(args)
        self.assertEqual(record['candidate']['pipeline'], self.base_id)  # Same pipeline, another sample.
        self.assertEqual(record['estimate']['new_calls']['finishing/review'], 1)
        self.assertEqual(appeal_runner.describe(args.root/'noise')['changes'], [])

    def test_retrospective_comparison_of_existing_runs(self):
        args = self.define('strict')
        appeal_runner.plan(args)
        with self.fake(), passthrough_extract():
            appeal_runner.execute(args)
        compare = SimpleNamespace(**vars(self.args), name='retro', base_run=self.root/'runs/production',
                                  candidate_run=args.root/'strict/run', cohort=None, hypothesis='Same cases, two runs')
        comparison = appeal_runner.retrospective(compare)
        self.assertEqual(comparison['impact']['selected']['cases'], 2)
        self.assertEqual(appeal_runner.load(args.root/'retro')['kind'], 'retrospective')
        with self.assertRaisesRegex(ValueError, 'nothing to run'):
            appeal_runner.execute(SimpleNamespace(**vars(compare), max_spend=1.0))


if __name__ == '__main__':
    unittest.main()
