"""Analytical semantics, immutable provenance, SQL boundary, and real MCP transports."""
import asyncio
from datetime import date
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from appeal_stats import research_stats
from research_snapshot import ResearchSnapshot, TABLES, build_snapshot, sha

ROOT = Path(__file__).resolve().parents[1]
HAS_MCP = importlib.util.find_spec('mcp') is not None


def bound(docket='A', start='2020-01-01', end='2020-06-01', sr='exact', er='exact'):
    kind = 'exact' if sr == er == 'exact' else 'at_least'
    days = (date.fromisoformat(end)-date.fromisoformat(start)).days if start and end else None
    if days is None or days < 0 or days == 0 and kind == 'at_least':
        kind, days = 'unknown', None
    return {'docket': docket,
        'start': {'date': start, 'relation': sr, 'description': 'SECRET narrative', 'evidence': [{'quote': 'SECRET quote'}]} if start else None,
        'end': {'date': end, 'relation': er, 'description': 'SECRET narrative', 'evidence': []} if end else None,
        'note': 'SECRET duration narrative', 'duration': {'kind': kind, 'days': days}}


def record(docket='A', reps=('pro_se',)):
    return {'docket': docket, 'project': {'municipality': 'Test Town', 'proposed_units': 10,
        'proposed_lots': 2, 'description': 'SECRET project', 'evidence': [{'quote': 'SECRET quote'}]},
        'municipal_decision': {'date': '2019-12', 'outcome': 'approved', 'description': 'SECRET decision'},
        'discretion': {'involved': 'yes', 'central': 'yes', 'description': 'SECRET contention'},
        'observed_status': {'litigation': 'resolved', 'permission': 'allowed', 'description': 'SECRET status'},
        'appellants': [{'name': 'Person '+str(n), 'relationship': 'neighbor', 'seeks': 'overturn_approval',
                       'representation': rep, 'evidence': [{'quote': 'SECRET quote'}]} for n, rep in enumerate(reps)],
        'related_dockets': [{'docket': 'Linked', 'relationship': 'SECRET narrative relationship'}],
        'timeline': [{'kind': 'filing', 'date': None, 'dockets': [], 'court_or_body': 'SECRET body', 'description': 'SECRET event'},
                     {'kind': 'resolution', 'date': '2020-06-01', 'dockets': [d for d in [docket, 'Linked'] if d is not None], 'description': 'SECRET event'}],
        'restrictions': [{'description': 'SECRET stop order', 'start': '2020', 'end': None}],
        'housing_changes': [{'description': 'SECRET imposed change'}]}


def result(records=None, scope=('yes', 'yes'), status='complete', route='extract', bounds=None):
    if records is None:
        records = [record()]
    return {'status': status, 'triage': {'municipal_appeal': scope[0], 'housing': scope[1], 'matter': 'SECRET scope'},
        'route': {'route': route, 'reason': 'SECRET routing'}, 'warnings': [],
        'candidate': {'schema_version': 'v6', 'dockets': records},
        'duration_bounds': bounds if bounds is not None else [bound(r['docket']) for r in records],
        'review': {'scope': {'verdict': 'agree', 'reason': 'SECRET review'}, 'flags': []}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.run = self.base/'run'
        self.run.mkdir()
        (self.run/'.lock').touch()
        (self.run/'results').mkdir()
        self.output = self.base/'snapshot'

    def write_run(self, results):
        manifest = {'name': 'test', 'schema_version': 'appeals-1', 'prompt_sha256': '0'*64,
                    'groups': [{'group_id': gid, 'eligible': True, 'documents': [{'title': 'SECRET doc'}],
                                'source_sha256': '1'*64} for gid in results]}
        (self.run/'manifest.json').write_text(json.dumps(manifest))
        for gid, value in results.items():
            if value is not None:
                (self.run/'results'/(gid+'.json')).write_text(json.dumps(value))

    def build(self, results=None):
        self.write_run(results or {'g-one': result()})
        self.manifest = build_snapshot(self.run, self.output)
        return ResearchSnapshot(self.output)

    def test_projection_physically_excludes_every_narrative(self):
        snap = self.build()
        self.assertNotIn(b'SECRET', (self.output/'facts.sqlite').read_bytes())
        self.assertNotIn('SECRET', (self.output/'manifest.json').read_text())
        for table, fields in TABLES.items():
            q = snap.query('SELECT * FROM '+table)
            self.assertEqual(q['columns'], [c for c, _, _ in fields])
        self.assertEqual(snap.query('SELECT event_date FROM timeline_events ORDER BY ordinal')['rows'], [[None], ['2020-06-01']])
        self.assertEqual(snap.query('SELECT start_date, end_date FROM restrictions')['rows'], [['2020', None]])

    def test_representation_and_scope_match_dashboard(self):
        fixtures = {'g-pro': result([record('A', ('pro_se',))]),
            'g-mixed': result([record('B', ('represented', 'pro_se'))]),
            'g-unknown': result([record('C', ('pro_se', 'unclear'))]),
            'g-empty': result([record('D', ())]),
            'g-excluded': result([record('E')], scope=('yes', 'no')),
            'g-route': result([record('F')], route='out_of_scope'),
            'g-uncertain': result([record('G')], scope=('unclear', 'yes')),
            'g-incomplete': result([record('H')], status='duration_failed'), 'g-pending': None}
        snap = self.build(fixtures)
        rows = snap.query('SELECT docket, representation FROM appeals ORDER BY docket')['rows']
        self.assertEqual(rows, [['A', 'pro_se'], ['B', 'mixed'], ['C', 'unclear'], ['D', 'unclear']])
        self.assertEqual(snap.query('SELECT docket FROM uncertain_appeals')['rows'], [['G']])
        self.assertEqual(snap.query('SELECT count(*) FROM all_appeals')['rows'], [[7]])
        self.assertEqual(snap.query("SELECT count(*) FROM appeals WHERE representation='pro_se'")['rows'], [[1]])
        cases, records = [], []
        for gid, r in fixtures.items():
            if r:
                cases.append({'group': gid, 'status': r['status'], 'scope': r['triage'], 'route': r['route']})
                records.extend({'group': gid, 'record': {**d, 'duration': b['duration']}}
                               for d, b in zip(r['candidate']['dockets'], r['duration_bounds']))
        stats = research_stats(cases, records)
        self.assertEqual(len(stats['appeals']), len(rows))
        self.assertEqual(len(stats['representation']['mixed']), 1)
        self.assertEqual(snap.query("SELECT status FROM coverage WHERE group_id='g-incomplete'")['rows'], [['duration_failed']])

    def test_duration_minimum_unknown_zero_and_reversed(self):
        bounds = [bound('A'), bound('B', sr='on_or_before'), bound('C', start=None),
                  bound('D', end='2020-01-01', sr='on_or_before'), bound('E', end='2019-01-01')]
        snap = self.build({'g-one': result([record(b['docket']) for b in bounds], bounds=bounds)})
        self.assertEqual(snap.query('SELECT duration_kind,duration_days FROM appeals ORDER BY docket')['rows'],
                         [['exact', 152], ['at_least', 152], ['unknown', None], ['unknown', None], ['unknown', None]])
        self.assertEqual(snap.query("SELECT avg(duration_days) FROM appeals WHERE duration_kind='exact'")['rows'], [[152.0]])

    def test_bad_arithmetic_and_duplicate_identity_fail_closed(self):
        self.write_run({'g-one': result(bounds=[{**bound(), 'duration': {'kind': 'exact', 'days': 999}}])})
        with self.assertRaisesRegex(ValueError, 'arithmetic'):
            build_snapshot(self.run, self.output)
        self.assertFalse(self.output.exists())
        self.write_run({'g-one': result([record(), record()])})
        with self.assertRaisesRegex(ValueError, 'Duplicate duration'):
            build_snapshot(self.run, self.output)

    def test_unknown_arrays_are_not_negative_observations_and_flags_survive(self):
        r = result()
        r['candidate']['dockets'][0]['housing_changes'] = None
        r['candidate']['dockets'][0]['restrictions'] = []
        r['warnings'] = ['SECRET warning', 'SECRET warning']
        r['review']['scope'] = {'verdict': 'flag', 'reason': 'SECRET scope concern'}
        r['review']['flags'] = [{'docket': 'A', 'issue': 'SECRET issue'}, {'docket': None, 'issue': 'SECRET issue'}]
        snap = self.build({'g-one': r})
        self.assertEqual(snap.query('SELECT housing_changes_state,housing_change_count,restrictions_state,restriction_count,review_required,review_flag_count FROM appeals')['rows'],
                         [['unknown', None, 'none_documented', 0, 1, 2]])
        self.assertEqual(snap.query('SELECT warning_count,review_flag_count,scope_review FROM coverage')['rows'], [[1, 2, 'flag']])
        self.assertNotIn(b'SECRET', snap.image)

    def test_newest_first_pass_excludes_unfinalized_facts(self):
        self.write_run({'g-one': result()})
        (self.run/'first_pass').mkdir()
        first = self.run/'first_pass/g-one.json'
        first.write_text(json.dumps(result(status='extracted')))
        os.utime(first, ns=(time.time_ns()+10**9,)*2)
        build_snapshot(self.run, self.output)
        snap = ResearchSnapshot(self.output)
        self.assertEqual(snap.query('SELECT count(*) FROM appeals')['rows'], [[0]])
        self.assertEqual(snap.query('SELECT status FROM coverage')['rows'], [['extracted']])

    def test_interrupted_activity_and_missing_scope_stay_explicit(self):
        self.write_run({'g-one': result(), 'g-missing': result(scope=(None, 'yes'))})
        (self.run/'activity').mkdir()
        activity = self.run/'activity/g-one.json'
        activity.write_text(json.dumps({'stage': 'reviewing'}))
        os.utime(activity, ns=(time.time_ns()+10**9,)*2)
        build_snapshot(self.run, self.output)
        snap = ResearchSnapshot(self.output)
        self.assertEqual(snap.query("SELECT status,finalized_docket_count FROM coverage WHERE group_id='g-one'")['rows'], [['interrupted', 0]])
        self.assertEqual(snap.query('SELECT count(*) FROM appeals')['rows'], [[0]])
        self.assertEqual(snap.query('SELECT review_required FROM uncertain_appeals')['rows'], [[1]])

    def test_writer_lock_overwrite_and_tampering(self):
        self.write_run({'g-one': result()})
        with (self.run/'.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'being written'):
                build_snapshot(self.run, self.output)
        build_snapshot(self.run, self.output)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            build_snapshot(self.run, self.output)
        db_path = self.output/'facts.sqlite'
        db_path.chmod(0o644)
        db_path.write_bytes(db_path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, 'altered'):
            ResearchSnapshot(self.output)

    def test_pinned_image_and_compact_description(self):
        snap = self.build()
        old_id = snap.manifest['snapshot_id']
        self.write_run({'g-one': result([record('Replacement')])})
        self.assertEqual(snap.query('SELECT docket FROM appeals')['rows'], [['A']])
        self.assertEqual(snap.query('SELECT 1')['snapshot_id'], old_id)
        snap.manifest['input_hashes'] *= 20000
        snap.manifest['source_hashes'] *= 20000
        self.assertLess(len(json.dumps(snap.describe())), 50000)
        self.assertNotIn('input_hashes', snap.describe()['snapshot'])

    def test_input_change_during_build_does_not_publish(self):
        self.write_run({'g-one': result()})
        from research_snapshot import load_run
        def changed(folder):
            data, provenance = load_run(folder)
            path = folder/'results/g-one.json'
            path.write_text(path.read_text()+' ')
            return data, provenance
        with patch('research_snapshot.load_run', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed'):
                build_snapshot(self.run, self.output)
        self.assertFalse(self.output.exists())

    def test_null_docket_remains_joinable(self):
        snap = self.build({'g-one': result([record(None)], bounds=[bound(None)])})
        self.assertEqual(snap.query('SELECT docket, count(*) OVER () FROM appeals')['rows'], [[None, 1]])
        self.assertEqual(snap.query('SELECT count(*) FROM appellants p JOIN appeals a USING(appeal_id)')['rows'], [[1]])

    def test_arbitrary_select_filters_joins_ctes_windows_and_parameters(self):
        snap = self.build({'g-one': result([record('A'), record('B', ('represented', 'pro_se'))])})
        q = snap.query('WITH chosen AS (SELECT a.* FROM appeals a WHERE municipality LIKE :town '
                      'AND EXISTS (SELECT 1 FROM appellants p WHERE p.appeal_id=a.appeal_id AND p.relationship=:role)) '
                      'SELECT docket, row_number() OVER(ORDER BY docket), count(*) OVER() FROM chosen',
                      {'town': 'Test%', 'role': 'neighbor'})
        self.assertEqual(q['rows'], [['A', 1, 2], ['B', 2, 2]])
        self.assertEqual(q['parameters']['role'], 'neighbor')
        self.assertFalse(q['truncated'])
        self.assertEqual(snap.query('SELECT docket AS same, permission AS same FROM appeals LIMIT 1')['columns'], ['same', 'same'])
        self.assertEqual(snap.query('SELECT count(*) FROM appeals WHERE docket=?', ["A' OR 1=1 --"])['rows'], [[0]])

    def test_adversarial_sql_denied_and_snapshot_unchanged(self):
        snap = self.build()
        for sql in ["DELETE FROM appeals", "UPDATE all_appeals SET docket='X'", "DROP TABLE all_appeals",
            "CREATE TABLE new(x)", "ATTACH DATABASE '/tmp/outside' AS outside", "DETACH main", 'PRAGMA database_list',
            "VACUUM INTO '/tmp/copy.sqlite'", 'BEGIN', 'COMMIT', 'SELECT * FROM sqlite_master',
            'SELECT count(*) FROM sqlite_master', 'SELECT * FROM pragma_table_info(\'all_appeals\')',
            'SELECT count(*) FROM pragma_table_info(\'all_appeals\')', "SELECT readfile('/etc/passwd')",
            "SELECT writefile('/tmp/output', 'x')", "SELECT load_extension('/tmp/x')", 'SELECT randomblob(1000000000)',
            'SELECT zeroblob(1000000000)', 'SELECT description FROM appeals', 'SELECT "motivation" FROM appeals',
            'SELECT evidence FROM appeals', 'SELECT 1; SELECT 2']:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                snap.query(sql)
        self.assertEqual(snap.query('SELECT count(*) FROM appeals')['rows'], [[1]])

    def test_query_resource_limits_and_truncation(self):
        snap = self.build()
        q = snap.query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10) SELECT x FROM n', max_rows=3)
        self.assertEqual(q['rows'], [[1], [2], [3]])
        self.assertEqual(q['truncation_reason'], 'row_limit')
        with self.assertRaises(ValueError):
            snap.query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n')
        q = snap.query("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<30) SELECT printf('%10000s','x') FROM n", max_rows=100)
        self.assertTrue(q['truncated'])
        self.assertEqual(q['truncation_reason'], 'byte_limit')
        self.assertLessEqual(len(json.dumps(q, ensure_ascii=False).encode()), 256000)
        for args in [('SELECT 1', [], 0), ('SELECT 1', [], 1001), ('SELECT 1', [], True),
            ('x'*16001, [], 1), ('SELECT ?', [[1]], 1), ('SELECT ?', ['x'*16001], 1),
            ('SELECT ?', [float('nan')], 1), ('SELECT ?', [2**80], 1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                snap.query(*args)


@unittest.skipUnless(HAS_MCP, 'Install the research-mcp extra to test actual transports')
class MCPTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        run = base/'run'
        (run/'results').mkdir(parents=True)
        (run/'.lock').touch()
        (run/'manifest.json').write_text(json.dumps({'name': 'protocol-fixture', 'schema_version': 'appeals-1',
            'prompt_sha256': '0'*64, 'groups': [{'group_id': 'g-one', 'documents': [], 'source_sha256': '1'*64}]}))
        (run/'results/g-one.json').write_text(json.dumps(result()))
        self.snapshot = base/'snapshot'
        build_snapshot(run, self.snapshot)

    async def assert_session(self, read, write):
        from mcp import ClientSession
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            self.assertIn('housing appeals', initialized.serverInfo.name)
            tools = await session.list_tools()
            self.assertEqual({tool.name for tool in tools.tools}, {'describe_dataset', 'query_sql'})
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools.tools))
            self.assertTrue(all(tool.outputSchema for tool in tools.tools))
            described = await session.call_tool('describe_dataset', {})
            self.assertFalse(described.isError)
            content = json.loads(described.content[0].text)
            self.assertEqual(content['snapshot']['run_name'], 'protocol-fixture')
            q = await session.call_tool('query_sql', {'sql': 'SELECT docket FROM appeals WHERE permission=?', 'parameters': ['allowed']})
            self.assertFalse(q.isError)
            self.assertEqual(json.loads(q.content[0].text)['rows'], [['A']])
            self.assertEqual(q.structuredContent['rows'], [['A']])
            rejected = await session.call_tool('query_sql', {'sql': "SELECT readfile('/etc/passwd')"})
            self.assertTrue(rejected.isError)
            self.assertNotIn('SECRET', rejected.content[0].text)
            recovered = await session.call_tool('query_sql', {'sql': 'SELECT count(*) FROM appeals'})
            self.assertEqual(json.loads(recovered.content[0].text)['rows'], [[1]])

    def test_stdio_initialize_discover_query_error_and_recovery(self):
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        config = json.loads(subprocess.check_output([sys.executable, str(ROOT/'research_mcp.py'),
            'client-config', '--snapshot', str(self.snapshot)], cwd=self.temp.name))
        entry = config['mcpServers']['housing-appeals']
        self.assertEqual(entry['command'], str(ROOT/'.venv/bin/python'))
        async def run():
            params = StdioServerParameters(command=entry['command'], args=entry['args'], cwd=str(self.temp.name))
            async with stdio_client(params) as (read, write):
                await self.assert_session(read, write)
        asyncio.run(asyncio.wait_for(run(), timeout=20))

    def test_loopback_http_initialize_discover_query_error_and_recovery(self):
        from mcp.client.streamable_http import streamable_http_client
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(ROOT/'research_mcp.py'), 'serve',
            '--snapshot', str(self.snapshot), '--transport', 'streamable-http', '--port', str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.temp.name)
        try:
            ready = False
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        ready = True
                        break
                except OSError:
                    if process.poll() is not None:
                        break
                    time.sleep(.05)
            self.assertTrue(ready, 'HTTP MCP server did not start')
            async def run():
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read, write, _):
                    await self.assert_session(read, write)
            asyncio.run(asyncio.wait_for(run(), timeout=20))
        finally:
            process.terminate()
            process.wait(timeout=5)
