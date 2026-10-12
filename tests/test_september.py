"""Regression against the September data, when it is present: the pipeline must reproduce it.

    cases     rebuilding the case set from September's documents and pages gives the same dataset
    triage    a sample of cases is answered entirely from the saved calls
    extract   with REPLAY_ALL=1, the whole extraction reproduces September's results from the cache
"""
import os
import tempfile
from pathlib import Path
import unittest

from court.core.datasets import Datasets, is_failure
from court.core.pipelines import Pipelines
from court.core.runner import Runner

STORE = Datasets()
PRESENT = all(name in STORE.names() for name in ('september-documents', 'september-pages', 'september-cases',
                                                 'september-results'))


@unittest.skipUnless(PRESENT, 'September datasets not imported (python -m court import-september)')
class SeptemberTests(unittest.TestCase):
    def setUp(self):
        # September's datasets are linked into a temporary store, so the runs here write nothing real.
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Datasets(root/'datasets')
        (root/'datasets').mkdir()
        for name, identity in STORE.names().items():
            if name.startswith('september-'):
                (root/'datasets'/identity).symlink_to(STORE.folder(identity))
                self.store.name(name, identity)
        self.runner = Runner(Pipelines(), self.store, root/'runs')
        self.pipeline = Pipelines().load('production')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_set_rebuilds_identically(self):
        record = self.runner.run('cases', self.pipeline, {'documents': 'september-documents',
                                                          'pages': 'september-pages'}, at='cases')
        self.assertEqual(record['output'], self.store.resolve('september-cases'))

    def test_triage_replays_from_saved_calls(self):
        keys = STORE.open('september-cases').keys()[::35]
        record = self.runner.run('triage', self.pipeline, {'cases': 'september-cases'}, at='extract/triage', keys=keys)
        self.assertEqual(record['items']['failed'], {})
        self.assertEqual(record['cost']['events'], {'reused': len(keys)})

    @unittest.skipUnless(os.getenv('REPLAY_ALL'), 'Set REPLAY_ALL=1 to replay all 1,395 cases (about 20 seconds)')
    def test_extraction_reproduces_september(self):
        record = self.runner.run('extract', self.pipeline, {'cases': 'september-cases'}, at='extract', workers=8)
        before, after = self.store.open('september-results').load(), self.store.open(record['output']).load()
        for key, value in before.items():
            if is_failure(value):
                self.assertTrue(is_failure(after[key]), key)
            else:
                self.assertEqual(after[key], value, key)


if __name__ == '__main__':
    unittest.main()
