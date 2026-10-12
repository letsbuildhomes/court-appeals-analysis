"""Research snapshots and the MCP server: structured facts only, bounded read-only SQL."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from court.config import ROOT
from court.contracts import results
from court.core.datasets import Failure
from court.research import snapshot as research

U = results.UNKNOWN
SECRET = 'SECRET narrative the snapshot must never hold'


def record(docket, representations=('represented',), days=None):
    cite = [{'doc': 1, 'page': 1, 'quote': SECRET}]
    duration = {'start': U, 'end': U, 'note': SECRET, 'kind': U, 'days': U}
    if days is not None:
        duration = {'start': {'date': '2020-01-01', 'relation': 'exact', 'description': SECRET, 'evidence': cite},
                    'end': {'date': '2020-01-11', 'relation': 'on_or_after', 'description': SECRET, 'evidence': cite},
                    'note': SECRET, 'kind': 'at_least', 'days': days}
    return {'docket': docket,
            'project': {'description': SECRET, 'municipality': 'Testville', 'proposed_units': 4, 'proposed_lots': U,
                        'evidence': cite},
            'municipal_decision': {'description': SECRET, 'date': '2019-05', 'outcome': 'approved', 'evidence': cite},
            'appellants': [{'name': f'Neighbor {i}', 'relationship': 'neighbor', 'seeks': 'overturn_approval',
                            'representation': r, 'evidence': cite} for i, r in enumerate(representations)],
            'discretion': {'description': SECRET, 'involved': 'yes', 'evidence': cite, 'central': 'no',
                           'central_evidence': cite},
            'observed_status': {'litigation': 'resolved', 'permission': 'allowed', 'description': SECRET,
                                'evidence': cite},
            'timeline': [{'kind': 'filing', 'date': '2019-06-01', 'court_or_body': SECRET, 'dockets': [docket],
                          'description': SECRET, 'evidence': cite}],
            'duration': duration, 'restrictions': [], 'housing_changes': [],
            'related_dockets': [{'docket': 'Z', 'relationship': SECRET, 'evidence': cite}]}


def disposition(value='in_scope'):
    return {'value': value, 'reason': SECRET, 'municipal_appeal': 'yes', 'housing': 'yes',
            'evidence': [{'doc': 1, 'page': 1, 'quote': SECRET}]}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, values=None, name='snap'):
        values = values or {'g-one': results.result('g-one', disposition(), [record('A', days=10)])}
        cases = {k: {'documents': [{}], 'dockets': ['A']} for k in values}
        research.build(values, cases, self.root/name, {'results': 'd-test', 'cases': 'd-cases', 'pipeline': None,
                                                       'run': None, 'contract': results.SCHEMA})
        return research.ResearchSnapshot(self.root/name)

    def test_projection_physically_excludes_every_narrative(self):
        self.build({'g-one': results.result('g-one', disposition(), [record('A', days=10)]),
                    'g-two': results.result('g-two', disposition('excluded')),
                    'g-three': Failure('validation', SECRET)})
        raw = (self.root/'snap'/'facts.sqlite').read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b'SECRET', (self.root/'snap'/'manifest.json').read_bytes())

    def test_cases_failures_dispositions_and_durations(self):
        snap = self.build({'g-one': results.result('g-one', disposition(), [record('A', ('represented', 'pro_se'), 10)]),
                           'g-two': results.result('g-two', disposition('uncertain'), [record('B')]),
                           'g-three': Failure('validation', 'bad answer')})
        rows = snap.query('SELECT case_id, status, failure_kind, disposition FROM cases ORDER BY case_id')['rows']
        self.assertEqual(rows, [['g-one', 'complete', None, 'in_scope'], ['g-three', 'failed', 'validation', None],
                                ['g-two', 'complete', None, 'uncertain']])
        self.assertEqual(snap.query('SELECT docket, representation, duration_kind, duration_days FROM appeals')['rows'],
                         [['A', 'mixed', 'at_least', 10]])
        self.assertEqual(snap.query('SELECT docket, duration_kind, duration_days, proposed_lots FROM uncertain_appeals')['rows'],
                         [['B', 'unknown', None, None]])

    def test_arbitrary_select_with_joins_and_parameters(self):
        snap = self.build({'g-one': results.result('g-one', disposition(), [record('A'), record('B', ('pro_se',))])})
        q = snap.query('WITH chosen AS (SELECT a.* FROM appeals a WHERE municipality LIKE :town AND EXISTS '
                       '(SELECT 1 FROM appellants p WHERE p.appeal_id=a.appeal_id AND p.representation=:rep)) '
                       'SELECT docket, row_number() OVER(ORDER BY docket) FROM chosen', {'town': 'Test%', 'rep': 'pro_se'})
        self.assertEqual(q['rows'], [['B', 1]])
        self.assertEqual(snap.query('SELECT count(*) FROM appeals WHERE docket=?', ["A' OR 1=1 --"])['rows'], [[0]])

    def test_adversarial_sql_is_denied(self):
        snap = self.build()
        for sql in ['DELETE FROM appeals', "UPDATE all_appeals SET docket='X'", 'DROP TABLE all_appeals',
                    "ATTACH DATABASE '/tmp/outside' AS outside", 'PRAGMA database_list', 'SELECT * FROM sqlite_master',
                    "SELECT * FROM pragma_table_info('all_appeals')", "SELECT readfile('/etc/passwd')",
                    "SELECT load_extension('/tmp/x')", 'SELECT randomblob(1000000000)', 'SELECT description FROM appeals',
                    'SELECT 1; SELECT 2']:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                snap.query(sql)
        q = snap.query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10) SELECT x FROM n',
                       max_rows=3)
        self.assertEqual((q['rows'], q['truncation_reason']), ([[1], [2], [3]], 'row_limit'))
        with self.assertRaises(ValueError):
            snap.query('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n')

    def test_tampering_and_overwrite_are_refused(self):
        self.build()
        with self.assertRaises(ValueError):
            self.build()
        path = self.root/'snap'/'facts.sqlite'
        path.chmod(0o644)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE all_appeals SET docket='X'")
        db.close()
        with self.assertRaises(ValueError):
            research.ResearchSnapshot(self.root/'snap')

    def test_the_container_needs_only_the_snapshot_reader_and_server(self):
        import shlex
        import shutil
        import subprocess
        dockerfile = (ROOT/'deploy/research/Dockerfile').read_text()
        files = [f for line in dockerfile.splitlines() if line.startswith('COPY court/')
                 for f in shlex.split(line)[1:-1]]
        for name in files:
            (self.root/'app'/name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, self.root/'app'/name)
        result = subprocess.run([sys.executable, '-m', 'court.research.mcp', '--help'], cwd=self.root/'app',
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_mcp_over_stdio(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.skipTest('Install the research-mcp extra')
        self.build()

        async def run():
            params = StdioServerParameters(command=sys.executable, args=['-m', 'court', 'research', 'serve', '--snapshot',
                                                                         str(self.root/'snap')], cwd=str(ROOT))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({t.name for t in tools.tools}, {'describe_dataset', 'query_sql'})
                    described = json.loads((await session.call_tool('describe_dataset', {})).content[0].text)
                    self.assertEqual(described['snapshot']['provenance']['results'], 'd-test')
                    q = await session.call_tool('query_sql', {'sql': 'SELECT docket FROM appeals'})
                    self.assertEqual(json.loads(q.content[0].text)['rows'], [['A']])
                    rejected = await session.call_tool('query_sql', {'sql': "SELECT readfile('/etc/passwd')"})
                    self.assertTrue(rejected.isError)
        asyncio.run(asyncio.wait_for(run(), timeout=30))


if __name__ == '__main__':
    unittest.main()
