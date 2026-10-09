import unittest

from appeal_demo import build_demo


class DemoTests(unittest.TestCase):
    def test_deterministic_software_test_with_no_execution(self):
        demo = build_demo()
        self.assertEqual(demo, build_demo())
        self.assertEqual(demo['purpose'], 'synthetic_software_test')
        self.assertIn('SYNTHETIC SOFTWARE TEST', demo['notice'])
        self.assertEqual(demo['execution']['new_model_calls'], 0)
        self.assertFalse(demo['execution']['performed'])
        self.assertEqual(demo['reference']['purpose'], 'synthetic_software_test')
        self.assertEqual(demo['evaluation']['schema_version'], 'appeal-evaluation-1')

    def test_recipe_and_progress_exercise_dashboard_without_execution(self):
        demo = build_demo()
        baseline, candidate = demo['baseline'], demo['candidate']
        before = {c['id']: c for c in baseline['recipe']['components']}
        after = {c['id']: c for c in candidate['recipe']['components']}
        self.assertIn('check', after.keys() - before.keys())
        self.assertNotEqual(before['extract']['prompt_sha256'], after['extract']['prompt_sha256'])
        self.assertEqual(before['finalize']['prompt_sha256'], after['finalize']['prompt_sha256'])
        self.assertNotEqual(before['finalize']['effective_prompt_sha256'], after['finalize']['effective_prompt_sha256'])
        self.assertTrue(any(c['execution']['status'] == 'reviewing' for c in candidate['cases']))
        self.assertTrue(candidate['provenance']['cost']['simulated'])
        self.assertIsNone(candidate['provenance']['cost']['estimated_usd'])
        self.assertEqual(demo['execution']['new_model_cost_usd'], 0)


if __name__ == '__main__':
    unittest.main()
