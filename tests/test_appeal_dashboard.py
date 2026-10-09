import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from appeal_dashboard import Dashboard, handler_for, ThreadingHTTPServer
from appeal_demo import build_demo


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = Dashboard()

    def test_demo_filters_and_detail(self):
        rows = self.dashboard.cases('demo', classification='corrected')
        self.assertGreater(rows['total'], 0)
        self.assertTrue(all(r['classifications']['corrected'] for r in rows['cases']))
        self.assertGreater(self.dashboard.cases('demo', classification='failures')['total'], 0)
        self.assertGreater(self.dashboard.cases('demo', status='running')['total'], 0)
        self.assertIn('synthetic-running', [r['case_id'] for r in self.dashboard.cases('demo', status='running')['cases']])
        self.assertNotIn('synthetic-interrupted', [r['case_id'] for r in self.dashboard.cases('demo', status='failed')['cases']])
        self.assertGreater(self.dashboard.cases('demo', q='SYNTHETIC-A')['total'], 0)
        self.assertEqual(len(self.dashboard.cases('demo', limit='1')['cases']), 1)
        detail = self.dashboard.detail('demo', rows['cases'][0]['case_id'])
        self.assertIsNotNone(detail['evaluation'])
        self.assertIsNotNone(detail['reference'])
        unclear = self.dashboard.cases('demo', classification='ambiguous')
        self.assertIn('synthetic-null', [r['case_id'] for r in unclear['cases']])
        with self.assertRaises(KeyError):
            self.dashboard.detail('demo', '../secret')
        for args in ({'offset': '-1'}, {'limit': '201'}, {'classification': 'fake'}):
            with self.assertRaises(ValueError):
                self.dashboard.cases('demo', **args)

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
        root = Path(temp)
        experiment = root / 'experiment'
        experiment.mkdir()
        source_run = root / 'source-run'
        (source_run / 'sources').mkdir(parents=True)
        canonical = build_demo()['baseline']
        canonical['cases'] = canonical['cases'][:1]
        case = canonical['cases'][0]
        case['source']['path'] = 'sources/case.txt'
        raw_source = b'frozen test text'
        case['source']['sha256'] = hashlib.sha256(raw_source).hexdigest()
        (source_run / 'sources/case.txt').write_bytes(raw_source)
        raw = json.dumps(canonical).encode()
        (experiment / 'cases.json').write_bytes(raw)
        manifest = {'schema_version': 'appeal-experiment-1', 'case_count': 1,
                    'source_run': str(source_run), 'artifacts': {'cases': {'path': 'cases.json',
                    'sha256': hashlib.sha256(raw).hexdigest(), 'schema_version': 'appeal-cases-1'}}}
        (experiment / 'manifest.json').write_text(json.dumps(manifest))
        return Dashboard(production=experiment), experiment, source_run

    def test_cache_validates_changed_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            dashboard, folder, source_run = self.make_production(temp)
            with patch('appeal_dashboard.read_experiment', wraps=__import__('appeal_experiments').read_experiment) as reader:
                a = dashboard.load('production')
                self.assertIs(a, dashboard.load('production'))
                self.assertEqual(reader.call_count, 1)
                (folder / 'cases.json').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'artifact changed'):
                    dashboard.load('production')
            self.assertEqual(a['reference'], None)
            self.assertEqual(a['evaluation'], None)

    def test_source_path_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            dashboard, _, source_run = self.make_production(temp)
            value = dashboard.load('production')
            case = value['baseline']['cases'][0]
            self.assertEqual(dashboard.source('production', case['case_id'])['text'], 'frozen test text')
            for unsafe in ('../private.txt', '/etc/passwd', 'sources/../../private.txt'):
                case['source']['path'] = unsafe
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    dashboard.source('production', case['case_id'])
            case['source']['path'] = 'sources/link.txt'
            (source_run / 'sources/link.txt').symlink_to(source_run / 'sources/case.txt')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                dashboard.source('production', case['case_id'])
            case['source']['path'] = 'sources/case.txt'
            (source_run / 'sources/case.txt').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                dashboard.source('production', case['case_id'])

    def test_http_guards_and_errors(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.dashboard))
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
            self.assertEqual(request('/api/catalog', {'Origin': 'http://evil.example'})[0], 403)
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
