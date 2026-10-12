"""The web app's API over a small lab: runs, datasets, items, pipelines, and the static files it serves."""
from pathlib import Path
import re
import unittest

from court.ui import server
from court.ui.api import Api
from court.lab.dashboard import Dashboard
from tests.test_lab import LabTests


class ApiTests(unittest.TestCase):
    setUp, tearDown = LabTests.setUp, LabTests.tearDown

    def test_every_section_reads(self):
        self.lab.new('looser', 'Shorter words count', inputs={'cases': self.words.id}, probe=['a'], confirm=['a', 'b'],
                     changes=[{'at': '', 'settings': {'longer_than': 2}}])
        self.lab.run('looser', 'probe', mode='cached')
        api = Api(Dashboard(self.lab))
        runs = api.runs()['runs']
        self.assertEqual({r['name'] for r in runs} >= {'full'}, True)
        candidate = next(r for r in runs if r['labels'].get('side') == 'candidate')
        self.assertEqual((candidate['labels']['experiment'], candidate['state']), ('looser', 'complete'))
        run = api.run('full')
        self.assertEqual(run['state'], 'complete')
        self.assertEqual([s['path'] for s in run['steps']], [''])
        items = api.run_items('full')
        self.assertEqual(items['states'], {'done': 4})
        home = api.home()
        self.assertEqual(home['experiments'][0]['id'], 'experiment:looser:probe')
        listing = api.datasets_list()['datasets']
        results = next(d for d in listing if d['kind'] == 'results' and d['run'] == 'full')
        view = api.dataset_view(results['id'])
        self.assertEqual((view['total'], view['report']['metrics']['cases']), (4, 4))
        item = api.item(results['id'], 'b')
        self.assertEqual(item['value']['disposition']['value'], 'in_scope')
        pipelines = api.pipelines_list()['pipelines']
        self.assertEqual(len(pipelines), 2)
        compared = api.pipeline(pipelines[0]['id'], pipelines[1]['id'])
        self.assertTrue(any(row['status'] == 'changed' for row in compared['pipelines']['diff']))


class StaticTests(unittest.TestCase):
    def test_the_page_loads_only_files_the_server_has(self):
        files = server.static()
        html = files['/'].read_text()
        assets = re.findall(r'src="([^"]+)"', html) + re.findall(r'rel="stylesheet" href="([^"]+)"', html)
        self.assertGreater(len(assets), 5)
        for name in assets:
            self.assertIn('/' + name, files)
        routes = server.routes.__code__.co_consts
        self.assertTrue(any('/api/run' == c for c in routes))


if __name__ == '__main__':
    unittest.main()
