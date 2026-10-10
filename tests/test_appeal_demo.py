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

    def test_pipelines_and_progress_exercise_dashboard_without_execution(self):
        demo = build_demo()
        rows = {r['path']: r for r in demo['pipelines']['diff']}
        self.assertEqual(rows['first-pass/check']['status'], 'added')
        self.assertEqual(rows['first-pass/extract']['prompts'][0]['status'], 'changed')
        self.assertEqual(rows['finalize']['status'], 'unchanged')
        self.assertNotEqual(demo['pipelines']['baseline']['id'], demo['pipelines']['candidate']['id'])
        self.assertTrue(any(c['status']['state'] == 'in_progress' for c in demo['candidate']['cases']))
        self.assertEqual(demo['execution']['new_model_cost_usd'], 0)


if __name__ == '__main__':
    unittest.main()
