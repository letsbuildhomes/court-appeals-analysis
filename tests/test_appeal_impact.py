"""Fact-level quality impact: directions, verdicts, roles and reference verification."""
import copy
import unittest
from unittest.mock import patch

import appeal_impact
from appeal_demo import build_demo
from appeal_impact import compare_case, impact, profile


def record(**changes):
    value = {'docket': 'A 1',
             'duration': {'start': {'date': '2020-01-01', 'relation': 'on_or_before'},
                          'end': {'date': '2020-01-31', 'relation': 'exact'}, 'kind': 'at_least', 'days': 30},
             'municipal_decision': {'outcome': 'approved', 'date': '2019-12-01', 'evidence': [{'doc': 1, 'page': 2, 'quote': 'approved'}]},
             'observed_status': {'litigation': 'resolved', 'permission': 'allowed'},
             'discretion': {'involved': 'yes', 'central': 'unknown'},
             'appellants': [{'name': 'X', 'representation': 'represented'}],
             'project': {'municipality': 'Town of Jericho', 'proposed_units': 4, 'proposed_lots': 'unknown'},
             'restrictions': [], 'housing_changes': [], 'related_dockets': [], 'description': 'Prose'}
    value.update(changes)
    return value


def case(records, status='complete', disposition='in_scope', case_id='c1', schema='appeal-result-1'):
    return {'case_id': case_id, 'status': {'state': status, 'errors': [], 'warnings': []}, 'schema': schema,
            'disposition': {'value': disposition, 'reason': 'r', 'municipal_appeal': 'yes', 'housing': 'yes', 'evidence': []},
            'records': records}


class ImpactTests(unittest.TestCase):
    def facts(self, result):
        return {f['key']: f for f in result['facts'] if f['record']}

    def test_fact_directions_ignore_wording_and_normalize_places(self):
        after = record(description='Different prose',
                       project={'municipality': 'Jericho, Vermont', 'proposed_units': 4, 'proposed_lots': 2},
                       observed_status={'litigation': 'ongoing', 'permission': 'unknown'},
                       duration={'start': {'date': '2020-01-01', 'relation': 'exact'},
                                 'end': {'date': '2020-01-31', 'relation': 'exact'}, 'kind': 'exact', 'days': 30},
                       discretion={'involved': 'yes', 'central': 'yes'})
        result = compare_case(case([record()]), case([after]))
        facts = self.facts(result)
        self.assertEqual(facts['municipality']['change'], 'unchanged')
        self.assertEqual(facts['lots']['change'], 'more_specific')
        self.assertEqual(facts['discretion_central']['change'], 'more_specific')
        self.assertEqual(facts['duration']['change'], 'more_specific')  # Exact is more specific than a minimum.
        self.assertEqual(facts['permission']['change'], 'less_specific')
        self.assertEqual(facts['litigation']['change'], 'different')
        self.assertEqual(result['verdict'], 'mixed')
        self.assertEqual(facts['decision_outcome']['evidence']['baseline'][0]['quote'], 'approved')
        wording = compare_case(case([record()]), case([record(description='Other prose')]))
        self.assertEqual((wording['verdict'], wording['chips']), ('unchanged', ['Wording or citations only']))

    def test_processing_records_and_scope_verdicts(self):
        recovered = compare_case(case([], status='failed'), case([record()]))
        self.assertEqual((recovered['verdict'], recovered['processing']['flow']), ('improved', 'recovered'))
        self.assertEqual(recovered['records']['gained'], ['A 1'])
        broke = compare_case(case([record()]), case([], status='failed'))
        self.assertEqual((broke['verdict'], broke['processing']['flow']), ('worsened', 'broke'))
        rescoped = compare_case(case([], disposition='excluded'), case([record()]))
        self.assertEqual(rescoped['verdict'], 'changed')  # Records follow the disposition; its change needs judgment.
        self.assertIn('Disposition excluded → in scope', rescoped['chips'])
        self.assertEqual(compare_case(case([record()]), case([record()]))['verdict'], 'unchanged')

    def test_generic_fields_from_other_producers(self):
        result = compare_case(case([{'docket': 'A', 'outcome': 'won', 'tags': ['b', 'a'], 'prose': 'x'}]),
                              case([{'docket': 'A', 'outcome': 'lost', 'tags': ['a', 'b'], 'prose': 'y'}]))
        facts = self.facts(result)
        self.assertEqual(facts['field:outcome']['change'], 'different')
        self.assertEqual(facts['field:tags']['change'], 'unchanged')
        self.assertNotIn('field:prose', facts)

    def test_facts_a_schema_did_not_measure_are_newly_measured(self):
        import appeal_schema
        later = appeal_schema.fact('review_standard', 'Decision & status', 'Review standard',
                                   lambda r: r.get('review_standard'), lambda r: [], since='appeal-result-2')
        with patch.object(appeal_schema, 'VERSIONS', ('appeal-result-1', 'appeal-result-2')), \
                patch('appeal_impact.RECORD_FACTS', appeal_impact.RECORD_FACTS + [later]):
            result = compare_case(case([record()]), case([record(review_standard='on_the_record')], schema='appeal-result-2'))
        fact = self.facts(result)['review_standard']
        self.assertEqual((fact['change'], fact['candidate']), ('newly_measured', 'on the record'))
        self.assertEqual(result['verdict'], 'unchanged')  # Newly measured is neither better nor worse.

    def test_reference_labels_verify_facts_and_roles_group_cases(self):
        demo = build_demo()
        result = impact(demo['baseline'], demo['candidate'], demo['evaluation'], demo['reference'],
                        roles={'synthetic-null': 'companion'})
        transition = next(c for c in result['cases'] if c['case_id'] == 'synthetic-transition')
        verified = {(f['record'], f['key']): f['verified'] for f in transition['facts'] if f['verified']}
        self.assertEqual(verified[('SYNTHETIC-A', 'duration')], 'regressed')
        self.assertEqual(verified[('SYNTHETIC-B', 'duration')], 'unchanged_correct')
        self.assertEqual(verified[(None, 'disposition')], 'corrected')
        self.assertTrue(transition['verified'])
        self.assertEqual(set(result['groups']), {'selected', 'companion'})
        self.assertEqual(result['groups']['companion']['cases'], 1)
        self.assertTrue(result['reference'])
        duration = next(r for r in result['groups']['selected']['fact_map'] if r['key'] == 'duration')
        self.assertEqual((duration['regressed'], duration['different']), (1, 0))  # Counted once, as verified.

    def test_profile_counts_missing_answers(self):
        cases = [case([record(), record(docket='B', duration={'start': 'unknown', 'end': 'unknown', 'kind': 'unknown',
                                                              'days': 'unknown'})]), case([], status='failed')]
        result = profile(cases)
        duration = next(f for f in result['facts'] if f['key'] == 'duration')
        self.assertEqual((duration['known'], duration['total']), (1, 2))
        self.assertEqual(result['metrics']['processed'], 1)
        self.assertEqual(result['statuses'], {'complete': 1, 'failed': 1})
        self.assertEqual(result['dispositions'], {'in_scope': 2})
        before = copy.deepcopy(cases)
        compare_case(cases[0], cases[1])
        self.assertEqual(cases, before)  # Comparison never mutates inputs.


if __name__ == '__main__':
    unittest.main()
