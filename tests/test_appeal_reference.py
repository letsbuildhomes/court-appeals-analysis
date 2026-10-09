import copy
import hashlib
import json
import unittest
from pathlib import Path

from appeal_reference import read_reference, validate_reference

FIXTURES = Path(__file__).parent / 'fixtures' / 'evaluation'


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.reference = read_reference(FIXTURES / 'reference.json')

    def test_valid_reference_returns_original(self):
        self.assertIs(validate_reference(self.reference), self.reference)
        self.assertEqual(self.reference['purpose'], 'synthetic_software_test')

    def test_unknown_and_adjudicated_null_are_distinct(self):
        fields = self.reference['cases'][0]['records'][0]['fields']
        unknown = next(f for f in fields if f['state'] == 'unknown')
        self.assertNotIn('value', unknown)
        self.assertTrue(any(f.get('value', 'absent') is None and f['state'] == 'adjudicated' for f in fields))

    def test_rejects_invalid_or_unsubstantiated_labels(self):
        for key, value in [('path', []), ('state', 'guessed'), ('comparison', 'fuzzy'), ('evidence', []), ('rationale', ''), ('version', ''), ('value', float('nan'))]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.reference)
                changed['cases'][0]['fields'][0][key] = value
                with self.assertRaises(ValueError):
                    validate_reference(changed)

    def test_adjudicated_value_required(self):
        del self.reference['cases'][0]['fields'][0]['value']
        with self.assertRaises(ValueError):
            validate_reference(self.reference)

    def test_duplicate_case_record_and_field_rejected(self):
        for level in ('cases', 'records', 'fields'):
            changed = copy.deepcopy(self.reference)
            items = changed['cases'] if level == 'cases' else changed['cases'][0][level]
            items.append(copy.deepcopy(items[0]))
            with self.subTest(level=level), self.assertRaises(ValueError):
                validate_reference(changed)

    def test_hash_identity_and_completeness_validation(self):
        for key, value in [('source_sha256', 'not-a-hash'), ('records_complete', 1), ('case_id', '')]:
            changed = copy.deepcopy(self.reference)
            changed['cases'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_reference(changed)

    def test_sources_are_frozen_and_hash_mismatch_is_deliberate(self):
        sources = json.loads((FIXTURES / 'sources.json').read_text())
        for case in self.reference['cases']:
            self.assertEqual(hashlib.sha256(sources[case['case_id']].encode()).hexdigest(), case['source_sha256'])
        candidate = json.loads((FIXTURES / 'candidate.json').read_text())
        mismatch = next(c for c in candidate['cases'] if c['case_id'] == 'synthetic-hash')
        self.assertNotEqual(mismatch['source']['sha256'], self.reference['cases'][4]['source_sha256'])

    def test_adjudicated_evidence_quotes_exist_in_frozen_sources(self):
        sources = json.loads((FIXTURES / 'sources.json').read_text())
        for case in self.reference['cases']:
            fields = case['fields'] + [field for record in case['records'] for field in record['fields']]
            for field in fields:
                if field['state'] != 'adjudicated':
                    continue
                for evidence in field['evidence']:
                    self.assertEqual(evidence['case_id'], case['case_id'])
                    self.assertIn(evidence['quote'], sources[evidence['case_id']])


if __name__ == '__main__':
    unittest.main()
