import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from appeal_dashboard import Dashboard, handler_for, ThreadingHTTPServer
from pipeline import runs
from pipeline.corpus import Corpus


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = Dashboard()

    def test_demo_filters_and_detail(self):
        rows = self.dashboard.cases('demo', verdict='mixed')
        self.assertGreater(rows['total'], 0)
        self.assertTrue(all(r['impact']['verdict'] == 'mixed' for r in rows['cases']))
        everything = self.dashboard.cases('demo')['cases']
        order = ('worsened', 'mixed', 'changed', 'improved', 'unchanged')
        self.assertEqual([r['impact']['verdict'] for r in everything],
                         sorted((r['impact']['verdict'] for r in everything), key=order.index))
        self.assertIn('synthetic-running', [r['case_id'] for r in self.dashboard.cases('demo', status='running')['cases']])
        failed = [r['case_id'] for r in self.dashboard.cases('demo', status='failed')['cases']]
        self.assertIn('synthetic-failure', failed)
        self.assertGreater(self.dashboard.cases('demo', q='SYNTHETIC-A')['total'], 0)
        self.assertEqual(len(self.dashboard.cases('demo', limit='1')['cases']), 1)
        detail = self.dashboard.detail('demo', rows['cases'][0]['case_id'])
        self.assertIsNotNone(detail['evaluation'])
        self.assertIsNotNone(detail['reference'])
        self.assertEqual(detail['impact']['verdict'], 'mixed')
        with self.assertRaises(KeyError):
            self.dashboard.detail('demo', '../secret')
        for args in ({'offset': '-1'}, {'limit': '201'}, {'verdict': 'fake'}, {'role': 'other'}, {'gap': 'fake'}):
            with self.assertRaises(ValueError):
                self.dashboard.cases('demo', **args)

    def test_catalog_and_overview_shape(self):
        datasets = {d['id']: d for d in self.dashboard.catalog()['datasets']}
        self.assertEqual(datasets['demo']['kind'], 'synthetic')
        self.assertEqual(datasets['demo']['impact']['cases'], 8)
        overview = self.dashboard.overview('demo')
        self.assertTrue(overview['impact']['reference'])
        self.assertNotIn('cases', overview['impact'])  # Case detail is fetched separately.

    def test_source_verified_and_mismatch_withheld(self):
        source = self.dashboard.source('demo', 'synthetic-transition')
        self.assertTrue(source['verified'])
        self.assertEqual(hashlib.sha256(source['text'].encode()).hexdigest(), source['sha256'])
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.dashboard.source('demo', 'synthetic-hash', 'candidate')
        self.dashboard.load('demo')['baseline']['cases'][0]['source']['sha256'] = None
        with self.assertRaisesRegex(ValueError, 'hash unavailable'):
            self.dashboard.source('demo', 'synthetic-transition')

    def make_production(self, temp):
        """A one-case corpus and a run over it, as the production run is laid out."""
        root = Path(temp)
        corpus = root/'corpus'
        (corpus/'sources').mkdir(parents=True)
        raw_source = b'frozen test text'
        (corpus/'sources/case.txt').write_bytes(raw_source)
        sha = hashlib.sha256(raw_source).hexdigest()
        (corpus/'manifest.json').write_text(json.dumps({'schema_version': 'appeals-1', 'groups': [
            {'group_id': 'case', 'dockets': ['1-1-01 Vtec'], 'documents': [], 'source_sha256': sha}]}))
        folder = root/'run'
        runs.create(folder, name='production', pipeline_id='p-000000000000', case_ids=['case'],
                    corpus=Corpus(corpus), kind='converted')
        result = {'schema': 'appeal-result-1', 'case': 'case', 'records': [],
                  'disposition': {'value': 'excluded', 'reason': 'Not housing', 'municipal_appeal': 'yes',
                                  'housing': 'no', 'evidence': []}}
        runs.write(folder, {'case': (result, {'state': 'complete', 'errors': [], 'warnings': []}, None)})
        return Dashboard(production=folder, experiments=root/'experiments'), folder, corpus

    def test_cache_validates_changed_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            dashboard, folder, _ = self.make_production(temp)
            with patch('appeal_dashboard.runs.load', wraps=runs.load) as reader:
                a = dashboard.load('production')
                self.assertIs(a, dashboard.load('production'))
                self.assertEqual(reader.call_count, 1)
                (folder/'executions.json').write_text('[{"started_at": "later"}]')
                self.assertIsNot(a, dashboard.load('production'))
                self.assertEqual(reader.call_count, 2)
            self.assertEqual(a['reference'], None)
            self.assertEqual(a['evaluation'], None)
            self.assertEqual(a['profile']['dispositions'], {'excluded': 1})

    def test_source_path_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            dashboard, _, corpus = self.make_production(temp)
            value = dashboard.load('production')
            case = value['baseline']['cases'][0]
            self.assertEqual(dashboard.source('production', case['case_id'])['text'], 'frozen test text')
            original = case['source']['path']
            for unsafe in ('../private.txt', '/etc/passwd', str(corpus/'sources/../../private.txt')):
                case['source']['path'] = unsafe
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    dashboard.source('production', case['case_id'])
            case['source']['path'] = original
            (corpus/'sources/case.txt').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                dashboard.source('production', case['case_id'])
            (corpus/'real.txt').write_bytes(b'frozen test text')
            (corpus/'sources/case.txt').unlink()
            (corpus/'sources/case.txt').symlink_to(corpus/'real.txt')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                dashboard.source('production', case['case_id'])

    def test_pipeline_tree_and_diff_in_overview(self):
        overview = self.dashboard.overview('demo')
        pipes = overview['pipelines']
        self.assertEqual(pipes['candidate']['children'][0]['children'][1]['name'], 'check')
        status = {row['path']: row['status'] for row in pipes['diff']}
        self.assertEqual((status['first-pass/extract'], status['first-pass/check']), ('changed', 'added'))
        prompt = next(r for r in pipes['diff'] if r['path'] == 'first-pass/extract')['prompts'][0]
        self.assertIn('citing a quotation', prompt['value'])
        self.assertIn('first-pass/check: added', overview['card']['variables'])

    def test_http_guards_and_errors(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.dashboard, ['lab.internal']))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        def request(path, headers=None, method='GET'):
            conn = HTTPConnection('127.0.0.1', port)
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            status, raw = response.status, response.read()
            conn.close()
            return status, raw
        try:
            self.assertEqual(request('/api/catalog')[0], 200)
            self.assertEqual(request('/api/catalog', {'Host': 'evil.example'})[0], 403)
            self.assertEqual(request('/api/catalog', {'Host': f'evil.example:{port}'})[0], 403)
            self.assertEqual(request('/api/catalog', {'Host': f'192.168.88.3:{port}'})[0], 200)
            self.assertEqual(request('/api/catalog', {'Host': f'[::1]:{port}'})[0], 200)
            self.assertEqual(request('/api/catalog', {'Host': f'Lab.Internal:{port}'})[0], 200)
            self.assertEqual(request('/api/catalog', {'Host': '192.168.88.3:1'})[0], 403)
            self.assertEqual(request('/api/catalog', {'Origin': 'http://evil.example'})[0], 403)
            lan = {'Host': f'192.168.88.3:{port}'}
            self.assertEqual(request('/api/catalog', {**lan, 'Origin': f'http://192.168.88.3:{port}'})[0], 200)
            self.assertEqual(request('/api/catalog', {**lan, 'Origin': f'http://127.0.0.1:{port}'})[0], 403)
            self.assertEqual(request('/api/catalog', {'Sec-Fetch-Site': 'cross-site'})[0], 403)
            self.assertEqual(request('/api/case?id=absent')[0], 404)
            self.assertEqual(request('/api/cases?dataset=demo&dataset=production')[0], 400)
            self.assertEqual(request('/api/cases?path=/etc/passwd')[0], 400)
            self.assertEqual(request('/../appeal_dashboard.py')[0], 404)
            self.assertEqual(request('/api/catalog', method='POST')[0], 501)
            status, raw = request('/api/source?id=synthetic-hash&side=candidate')
            self.assertEqual(status, 400)
            self.assertNotIn('text', json.loads(raw))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
