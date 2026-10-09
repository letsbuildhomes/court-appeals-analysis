import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from appeal_contracts import CONTRACT_VERSION, adapt_run


class ContractsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder/'.lock').touch()
        for sub in ('sources', 'results', 'first_pass', 'activity', 'calls'):
            (self.folder/sub).mkdir()
        prompts = {'extract': 'frozen'}
        self.write('prompts.json', prompts)
        groups = []
        for gid in ('complete', 'excluded', 'failed', 'uncertain', 'not_run'):
            (self.folder/'sources'/f'{gid}.txt').write_text(gid)
            groups.append({'group_id': gid, 'source_sha256': hashlib.sha256(gid.encode()).hexdigest(), 'documents': [], 'dockets': []})
        self.write('manifest.json', {'schema_version': 'appeals-1', 'name': 'fixture', 'groups': groups, 'prompt_sha256': hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest()})
        self.write('results/complete.json', {'status': 'complete', 'triage': {'municipal_appeal': 'yes', 'housing': 'yes'}, 'candidate': {'schema_version': 'v6', 'dockets': [{'docket': 'D', 'scouting': 'draft'}]}, 'duration_bounds': [{'docket': 'D', 'duration': {'days': 3}, 'start': None}]})
        self.write('results/excluded.json', {'status': 'complete', 'route': {'route': 'out_of_scope'}})
        self.write('results/failed.json', {'status': 'review_failed', 'candidate': {'schema_version': 'v6', 'dockets': [{'docket': 'draft'}]}})
        self.write('results/uncertain.json', {'status': 'scope_flagged', 'triage': {'housing': 'unclear'}})

    def write(self, name, value):
        (self.folder/name).write_text(json.dumps(value))

    def test_coverage_and_finalized_records(self):
        value = adapt_run(self.folder)
        self.assertEqual(value['schema_version'], CONTRACT_VERSION)
        cases = {c['case_id']: c for c in value['cases']}
        self.assertEqual(len(cases), 5)
        self.assertEqual(cases['complete']['records'], [{'docket': 'D', 'duration': {'days': 3}, 'duration_bounds': {'start': None}}])
        self.assertEqual(cases['failed']['records'], [])
        self.assertIsNotNone(cases['failed']['candidate'])
        self.assertEqual(cases['excluded']['scope_category'], 'excluded')
        self.assertEqual(cases['uncertain']['scope_category'], 'uncertain')
        self.assertEqual(cases['not_run']['execution']['status'], 'not_run')
        self.assertIsNone(value['provenance']['cost']['recorded_usd'])

    def test_missing_bounds_rejected(self):
        result = json.loads((self.folder/'results/complete.json').read_text())
        result['duration_bounds'] = []
        self.write('results/complete.json', result)
        with self.assertRaisesRegex(ValueError, 'exactly matching'):
            adapt_run(self.folder)

    def test_null_docket_identity_preserved(self):
        self.write('results/complete.json', {'status': 'complete', 'candidate': {'schema_version': 'v6', 'dockets': [{'docket': None}]}, 'duration_bounds': [{'docket': None, 'duration': {'days': None}}]})
        self.assertIsNone(adapt_run(self.folder)['cases'][0]['records'][0]['docket'])

    def test_source_hash_rejected_or_explicitly_unverified(self):
        (self.folder/'sources/failed.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Frozen source'):
            adapt_run(self.folder)
        self.assertFalse(adapt_run(self.folder, verify_sources=False)['provenance']['sources_verified'])

    def test_newer_first_pass_and_activity_suppress_final_records(self):
        self.write('first_pass/complete.json', {'status': 'extracted', 'candidate': {'schema_version': 'v6', 'dockets': [{'docket': 'draft'}]}})
        os.utime(self.folder/'results/complete.json', ns=(1, 1))
        value = adapt_run(self.folder)['cases'][0]
        self.assertEqual(value['execution']['checkpoint_status'], 'extracted')
        self.assertEqual(value['records'], [])
        self.write('activity/complete.json', {'stage': 'reviewing'})
        os.utime(self.folder/'first_pass/complete.json', ns=(2, 2))
        self.assertEqual(adapt_run(self.folder)['cases'][0]['execution']['status'], 'interrupted')

    def test_inherited_partial_cost(self):
        self.write('calls/a.result.json', {'charged_or_reserved_usd': 2.5})
        self.write('calls/b.result.json', {'status': 'failed'})
        self.assertEqual(adapt_run(self.folder)['provenance']['cost'], {'kind': 'inherited', 'recorded_usd': 2.5, 'completeness': 'partial'})


if __name__ == '__main__':
    unittest.main()
