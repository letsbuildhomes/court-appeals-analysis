"""Protect research denominators and missing-data semantics."""
import unittest

from appeal_stats import research_stats


class ResearchStatsTests(unittest.TestCase):
    def case(self, group='g-a', status='complete', housing='yes', municipal='yes'):
        return {'group': group, 'status': status,
                'scope': {'housing': housing, 'municipal_appeal': municipal}}

    def row(self, docket, group='g-a', **fields):
        return {'group': group, 'record': {'docket': docket, **fields}}

    def test_missing_or_invalid_duration_is_not_zero_and_known_zero_is_valid(self):
        records = [self.row('zero', duration={'kind': 'exact', 'days': 0}),
                   self.row('minimum', duration={'kind': 'at_least', 'days': 100}),
                   self.row('missing'),
                   self.row('null', duration={'kind': 'exact', 'days': None}),
                   self.row('string', duration={'kind': 'exact', 'days': '100'}),
                   self.row('negative', duration={'kind': 'at_least', 'days': -1}),
                   self.row('unknown', duration={'kind': 'unknown', 'days': 900})]
        stats = research_stats([self.case()], records)['duration']
        self.assertEqual(stats['mean_supported_days'], 50)
        self.assertEqual(stats['mean_exact_days'], 0)
        self.assertEqual(len(stats['unknown']), 5)
        empty = research_stats([self.case()], [self.row('missing')])['duration']
        self.assertIsNone(empty['mean_supported_days'])
        self.assertIsNone(empty['mean_exact_days'])

    def test_scope_completion_and_docket_grain(self):
        cases = [self.case(), self.case('g-u', housing='unclear'),
                 self.case('g-p', status='reviewing'), self.case('g-x', municipal='no')]
        records = [self.row('A'), self.row('B'), self.row('A'),
                   self.row('U', 'g-u'), self.row('P', 'g-p'), self.row('X', 'g-x')]
        stats = research_stats(cases, records)
        self.assertEqual(len(stats['appeals']), 2)
        self.assertEqual(stats['groups'], 1)
        self.assertEqual(stats['uncertain_scope'], [{'group': 'g-u', 'docket': 'U'}])
        self.assertEqual(len(stats['discretion']['unclear']), 2)
        self.assertEqual(stats['discretion']['no'], [])
        self.assertEqual(len(stats['permission']['unclear']), 2)

    def test_empty_findings_are_not_missing_findings_or_proof_of_no_impact(self):
        records = [self.row('documented', housing_changes=[{'description': 'Condition'}], restrictions=[]),
                   self.row('empty', housing_changes=[], restrictions=[{'description': 'Stay'}]),
                   self.row('unknown')]
        stats = research_stats([self.case()], records)
        for field in ['housing_changes', 'restrictions']:
            self.assertEqual({key: len(rows) for key, rows in stats['impacts'][field].items()},
                             {'documented': 1, 'none_documented': 1, 'unknown': 1})

    def test_representation_requires_every_appellant_status(self):
        statuses = [('represented', 'represented'), ('pro_se',),
                    ('represented', 'pro_se'), ('represented', 'unclear'), ()]
        records = [self.row(str(i), appellants=[{'representation': value} for value in values])
                   for i, values in enumerate(statuses)]
        stats = research_stats([self.case()], records)['representation']
        self.assertEqual({key: len(rows) for key, rows in stats.items()},
                         {'represented': 1, 'pro_se': 1, 'mixed': 1, 'unclear': 2})

    def test_bucket_boundaries_and_primary_issue_denominator(self):
        days = [0, 90, 91, 180, 181, 365, 366, 730, 731, 1460, 1461]
        records = [self.row(str(day), duration={'kind': 'at_least', 'days': day},
                            discretion={'involved': 'yes', 'central': 'unclear'}) for day in days]
        records.append(self.row('no discretion', discretion={'involved': 'no', 'central': 'not_applicable'}))
        stats = research_stats([self.case()], records)
        self.assertEqual([len(b['at_least']) for b in stats['duration']['buckets']], [2, 2, 2, 2, 2, 1])
        self.assertEqual(len(stats['centrality']['unclear']), 11)
        self.assertEqual(sum(map(len, stats['centrality'].values())), len(stats['discretion']['yes']))


if __name__ == '__main__':
    unittest.main()
