import copy
import unittest

from appeal_assembly import effective_status, finalized_dockets, latest_checkpoint


class AssemblyTests(unittest.TestCase):
    def test_checkpoint_ties_and_missing(self):
        self.assertEqual(latest_checkpoint(['results', 'first_pass'], {'results': 2, 'first_pass': 2}), 'results')
        self.assertEqual(latest_checkpoint(['results', 'first_pass'], {'first_pass': 3}), 'first_pass')
        self.assertIsNone(latest_checkpoint(['results'], {}))

    def test_activity_and_eligibility(self):
        args = dict(checkpoint_time=2, activity_time=3, activity_stage='reviewing')
        self.assertEqual(effective_status('complete', **args), 'interrupted')
        self.assertEqual(effective_status('complete', running=True, **args), 'reviewing')
        self.assertEqual(effective_status('complete', eligible=False, **args), 'too_large')
        self.assertEqual(effective_status('complete', checkpoint_time=3, activity_time=3,
                                          activity_stage='reviewing'), 'complete')

    def test_duration_policies_and_copy(self):
        records = [{'docket': 'a', 'scouting': {}, 'uncertainties': [], 'suggestions': [], 'project': {}}]
        bounds = [{'docket': 'a', 'duration': {'kind': 'unknown', 'days': None}, 'start': None}]
        original = copy.deepcopy((records, bounds))
        clean = finalized_dockets(records, bounds)[0]
        self.assertEqual(clean, {'docket': 'a', 'project': {}, 'duration': bounds[0]['duration'],
                                 'duration_bounds': {'start': None}})
        self.assertEqual((records, bounds), original)
        self.assertEqual(finalized_dockets(records, [], missing='skip'), [])
        with self.assertRaises(KeyError):finalized_dockets(records, [])
        with self.assertRaisesRegex(ValueError, 'exactly matching'):finalized_dockets(records, [], strict=True)
        with self.assertRaisesRegex(ValueError, 'Duplicate duration'):finalized_dockets([], bounds * 2, strict=True)
        with self.assertRaisesRegex(ValueError, 'Duplicate docket'):finalized_dockets(records * 2, bounds, strict=True)
