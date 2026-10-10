"""Fact-level quality impact: directions, verdicts, roles and reference verification."""
import copy
import unittest

from appeal_demo import build_demo
from appeal_impact import compare_case, impact, profile


def record(**changes):
    value = {'docket': 'A 1', 'duration': {'kind': 'at_least', 'days': 30},
             'duration_bounds': {'start': {'date': '2020-01-01', 'relation': 'on_or_before'},
                                 'end': {'date': '2020-01-31', 'relation': 'exact'}},
             'municipal_decision': {'outcome': 'approved', 'date': '2019-12-01', 'evidence': [{'doc': 1, 'page': 2, 'quote': 'approved'}]},
             'observed_status': {'litigation': 'resolved', 'permission': 'allowed'},
             'discretion': {'involved': 'yes', 'central': 'unclear'},
             'appellants': [{'name': 'X', 'representation': 'represented'}],
             'project': {'municipality': 'Town of Jericho', 'proposed_units': 4, 'proposed_lots': None},
             'restrictions': [], 'housing_changes': [], 'related_dockets': [], 'description': 'Prose'}
    value.update(changes)
    return value


def case(records, status='complete', scope='confirmed', case_id='c1'):
    return {'case_id': case_id, 'execution': {'status': status, 'warnings': []}, 'scope_category': scope,
            'scope': {'municipal_appeal': 'yes', 'housing': 'yes'}, 'records': records}


class ImpactTests(unittest.TestCase):
    def facts(self, result):
        return {f['key']: f for f in result['facts'] if f['record']}

    def test_fact_directions_ignore_wording_and_normalize_places(self):
        after = record(description='Different prose',
                       project={'municipality': 'Jericho, Vermont', 'proposed_units': 4, 'proposed_lots': 2},
                       observed_status={'litigation': 'ongoing', 'permission': None},
                       duration={'kind': 'exact', 'days': 30},
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
        recovered = compare_case(case([], status='review_failed'), case([record()]))
        self.assertEqual((recovered['verdict'], recovered['processing']['flow']), ('improved', 'recovered'))
        self.assertEqual(recovered['records']['gained'], ['A 1'])
        broke = compare_case(case([record()]), case([], status='duration_failed'))
        self.assertEqual((broke['verdict'], broke['processing']['flow']), ('worsened', 'broke'))
        rescoped = compare_case(case([], scope='excluded'), case([record()]))
        self.assertEqual(rescoped['verdict'], 'changed')  # Records follow scope; a scope change needs judgment.
        self.assertIn('Scope excluded → confirmed', rescoped['chips'])
        self.assertEqual(compare_case(case([record()]), case([record()]))['verdict'], 'unchanged')

    def test_generic_fields_from_other_producers(self):
        result = compare_case(case([{'docket': 'A', 'outcome': 'won', 'tags': ['b', 'a'], 'prose': 'x'}]),
                              case([{'docket': 'A', 'outcome': 'lost', 'tags': ['a', 'b'], 'prose': 'y'}]))
        facts = self.facts(result)
        self.assertEqual(facts['field:outcome']['change'], 'different')
        self.assertEqual(facts['field:tags']['change'], 'unchanged')
        self.assertNotIn('field:prose', facts)

    def test_reference_labels_verify_facts_and_roles_group_cases(self):
        demo = build_demo()
        result = impact(demo['baseline'], demo['candidate'], demo['evaluation'], demo['reference'],
                        roles={'synthetic-null': 'companion'})
        transition = next(c for c in result['cases'] if c['case_id'] == 'synthetic-transition')
        verified = {(f['record'], f['key']): f['verified'] for f in transition['facts'] if f['verified']}
        self.assertEqual(verified[('SYNTHETIC-A', 'duration')], 'regressed')
        self.assertEqual(verified[('SYNTHETIC-B', 'duration')], 'unchanged_correct')
        self.assertEqual(verified[(None, 'scope_category')], 'corrected')
        self.assertTrue(transition['verified'])
        self.assertEqual(set(result['groups']), {'selected', 'companion'})
        self.assertEqual(result['groups']['companion']['cases'], 1)
        self.assertTrue(result['reference'])
        duration = next(r for r in result['groups']['selected']['fact_map'] if r['key'] == 'duration')
        self.assertEqual((duration['regressed'], duration['different']), (1, 0))  # Counted once, as verified.

    def test_profile_counts_missing_answers(self):
        cases = [case([record(), record(docket='B', duration={'kind': 'unknown', 'days': None})]), case([], status='review_failed')]
        result = profile(cases)
        duration = next(f for f in result['facts'] if f['key'] == 'duration')
        self.assertEqual((duration['known'], duration['total']), (1, 2))
        self.assertEqual(result['metrics']['processed'], 1)
        self.assertEqual(result['statuses'], {'complete': 1, 'review_failed': 1})
        before = copy.deepcopy(cases)
        compare_case(cases[0], cases[1])
        self.assertEqual(cases, before)  # Comparison never mutates inputs.


if __name__ == '__main__':
    unittest.main()
