import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from appeal_experiments import create_experiment, read_experiment


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.run = self.base/'run'
        self.run.mkdir()
        self.output = self.base/'experiment'
        self.canonical = {'schema_version': 'appeal-cases-1',
            'recipe': {'name': 'historical'},
            'cases': [{'case_id': 'G1', 'execution': {'status': 'review_failed'},
                       'records': []}]}

    def test_roundtrip_no_model_execution_and_no_reference(self):
        with patch('appeal_experiments.adapt_run', return_value=self.canonical) as adapt:
            create_experiment(self.run, self.output, code_revision='abc')
        adapt.assert_called_once_with(self.run.resolve(), verify_sources=True)
        loaded = read_experiment(self.output)
        self.assertEqual(loaded['cases'], self.canonical['cases'])
        self.assertEqual(loaded['manifest']['execution']['new_model_calls'], 0)
        self.assertIsNone(loaded['manifest']['reference'])
        self.assertEqual(loaded['manifest']['adapter_revision'], 'abc')
        self.assertEqual(list(self.run.iterdir()), [])

    def test_refuses_overwrite_and_source_output(self):
        self.output.mkdir()
        sentinel = self.output/'keep'
        sentinel.write_text('original')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            create_experiment(self.run, self.output)
        self.assertEqual(sentinel.read_text(), 'original')
        with self.assertRaisesRegex(ValueError, 'outside'):
            create_experiment(self.run, self.run/'experiment')

    def test_detects_modified_cases_and_unsafe_manifest_path(self):
        with patch('appeal_experiments.adapt_run', return_value=self.canonical):
            create_experiment(self.run, self.output)
        cases = self.output/'cases.json'
        cases.write_text(cases.read_text()+' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            read_experiment(self.output)
        manifest_path = self.output/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['artifacts']['cases']['path'] = '../outside.json'
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'path'):
            read_experiment(self.output)


if __name__ == '__main__':
    unittest.main()
