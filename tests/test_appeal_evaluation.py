import copy
import unittest

from appeal_evaluation import evaluate


def fixture(values, docket='AB-1'):
    return {'schema_version': 'appeal-cases-1', 'cases': [{
        'case_id': 'c', 'source': {'sha256': 'a' * 64}, 'execution': {'status': 'complete'},
        'scope_category': 'appeal', 'records': [{'docket': docket, **values}]}]}


def reference(labels, docket='AB-1'):
    return {'schema_version': 'appeal-reference-1', 'reference_id': 'test', 'version': '1',
            'purpose': 'synthetic_software_test', 'cases': [{'case_id': 'c', 'source_sha256': 'a' * 64,
            'adjudication': {'state': 'adjudicated', 'rationale': 'fixture'}, 'records_complete': True, 'fields': [],
            'records': [{'record_id': 'r', 'identity': {'docket': docket}, 'fields': [
                {'path': [key], 'state': state, 'comparison': comparison, 'evidence': ['synthetic fixture'],
                 'rationale': 'fixture', 'version': '1', **({'value': value} if state == 'adjudicated' else {})}
                for key, value, state, comparison in labels]}]}]}


class EvaluationTests(unittest.TestCase):
    def test_classifications_and_known_null(self):
        b = fixture({'a': 0, 'b': 1, 'c': 0, 'd': None, 'e': 'before'})
        c = fixture({'a': 1, 'b': 0, 'c': 2, 'd': None, 'e': 'after'})
        r = reference([(k, v, state, 'exact') for k, v, state in
                       [('a', 1, 'adjudicated'), ('b', 1, 'adjudicated'), ('c', 1, 'adjudicated'),
                        ('d', None, 'adjudicated'), ('e', None, 'unknown')]])
        out = evaluate(b, c, r)
        self.assertEqual([f['classification'] for f in out['cases'][0]['fields'] if f['path'][0] in 'abcde'],
                         ['corrected', 'regressed', 'unchanged_error', 'unchanged_correct', 'changed_unscored'])
        self.assertEqual(out['summary']['comparable_fields'], 4)
        self.assertEqual(out['summary']['candidate_accuracy'], .5)

    def test_missing_field_distinct_from_unknown(self):
        out = evaluate(fixture({}), fixture({'x': 'unknown'}), reference([('x', 'unknown', 'adjudicated', 'exact')]))
        field = next(f for f in out['cases'][0]['fields'] if f['path'] == ['x'])
        self.assertFalse(field['baseline']['present'])
        self.assertEqual(field['classification'], 'corrected')

    def test_unordered_structured_and_prose(self):
        out = evaluate(fixture({'x': [{'a': [1, 2]}, {'b': 3}], 'p': 'old'}),
                       fixture({'x': [{'b': 3}, {'a': [2, 1]}], 'p': 'new'}),
                       reference([('x', [{'a': [1, 2]}, {'b': 3}], 'adjudicated', 'unordered'),
                                  ('p', 'new', 'adjudicated', 'prose')]))
        self.assertEqual(out['summary']['candidate_accuracy'], 1)
        self.assertFalse(next(f for f in out['cases'][0]['fields'] if f['path'] == ['p'])['scored'])

    def test_hash_incompatibility(self):
        c = fixture({'x': 1})
        c['cases'][0]['source']['sha256'] = 'b' * 64
        out = evaluate(fixture({'x': 0}), c, reference([('x', 1, 'adjudicated', 'exact')]))
        self.assertIn('candidate:source_hash_mismatch', out['cases'][0]['compatibility']['reasons'])
        self.assertIsNone(out['summary']['candidate_accuracy'])
        self.assertEqual(out['summary']['label_coverage'], 0)

    def test_record_order_missing_and_duplicates(self):
        b = fixture({'x': 1}, ' ab-1 ')
        c = fixture({'x': 1}, 'AB1')
        out = evaluate(b, c, reference([('x', 1, 'adjudicated', 'exact')]))
        self.assertEqual(out['summary']['comparable_fields'], 0)
        self.assertEqual(out['cases'][0]['records']['candidate']['missing'], ['r'])
        c = copy.deepcopy(b)
        c['cases'][0]['records'].append(copy.deepcopy(c['cases'][0]['records'][0]))
        out = evaluate(b, c, reference([('x', 1, 'adjudicated', 'exact')]))
        self.assertEqual(len(out['cases'][0]['records']['candidate']['ambiguous']), 1)

    def test_null_singleton_and_pending_case(self):
        r = reference([('x', 1, 'adjudicated', 'exact')], None)
        out = evaluate(fixture({'x': 1}, None), fixture({'x': 1}, None), r)
        self.assertEqual(out['cases'][0]['records']['baseline']['null_singleton_matches'], ['r'])
        self.assertEqual(out['summary']['comparable_fields'], 0)
        self.assertEqual(next(f for f in out['cases'][0]['fields'] if f['path'] == ['x'])['unscored_reason'], 'anonymous_record_identity_unconfirmed')
        r['cases'][0]['adjudication']['state'] = 'pending'
        out = evaluate(fixture({'x': 1}, None), fixture({'x': 1}, None), r)
        self.assertIsNone(out['summary']['baseline_accuracy'])

    def test_no_reference_and_wrapped_input(self):
        b, c = fixture({'x': 1}), fixture({'x': 2})
        out = evaluate({'canonical': b, 'cases': b['cases']}, c)
        self.assertIsNone(out['summary']['candidate_accuracy'])
        self.assertEqual(out['summary']['eligible_labels'], 0)
        self.assertIn('changed_unscored', out['summary']['classifications'])

    def test_partial_reference_preserves_unlabelled_records(self):
        b, c = fixture({'x': 1}), fixture({'x': 1})
        b['cases'][0]['records'].append({'docket': 'OTHER', 'result': 'old'})
        c['cases'][0]['records'].insert(0, {'docket': 'OTHER', 'result': 'new'})
        r = reference([('x', 1, 'adjudicated', 'exact')])
        r['cases'][0]['records_complete'] = False
        out = evaluate(b, c, r)
        self.assertEqual(out['summary']['comparable_fields'], 1)
        changed = [f for f in out['cases'][0]['fields'] if f['classification'] == 'changed_unscored']
        self.assertEqual(changed[0]['path'], ['result'])
        self.assertFalse(out['cases'][0]['records']['candidate']['additional'][0]['scored'])

    def test_no_reference_list_order_and_duplicate_multiplicity(self):
        out = evaluate(fixture({'x': [1, 2, 2]}), fixture({'x': [2, 1, 2]}))
        self.assertEqual(out['summary']['classifications'].get('changed_unscored', 0), 0)
        out = evaluate(fixture({'x': [1, 2, 2]}), fixture({'x': [2, 1]}))
        self.assertEqual(out['summary']['classifications']['changed_unscored'], 1)

    def test_missing_case_and_hash_explicit(self):
        b = fixture({'x': 1})
        c = fixture({'x': 1})
        c['cases'][0]['source'] = {}
        out = evaluate(b, c, reference([('x', 1, 'adjudicated', 'exact')]))
        self.assertIn('candidate:missing_source_hash', out['cases'][0]['compatibility']['reasons'])
        c['cases'] = []
        out = evaluate(b, c, reference([('x', 1, 'adjudicated', 'exact')]))
        self.assertIn('candidate:missing_case', out['cases'][0]['compatibility']['reasons'])


if __name__ == '__main__':
    unittest.main()
