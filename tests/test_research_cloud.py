"""Portable snapshots and plain HTTP access."""
import asyncio
import os
from pathlib import Path
import stat
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from research_mcp import allowed_host, http_app, make_server, publish_snapshot
from research_snapshot import ResearchSnapshot
from tests import test_research_mcp as fixtures


class PublishTests(unittest.TestCase):
    setUp = fixtures.SnapshotTests.setUp
    build = fixtures.SnapshotTests.build
    write_run = fixtures.SnapshotTests.write_run

    def test_container_code_imports_without_the_repository(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root/'deploy/research/Dockerfile').read_text()
        files = next(shlex.split(line)[1:-1] for line in dockerfile.splitlines()
                     if line.startswith('COPY research_mcp.py '))
        with tempfile.TemporaryDirectory() as temp:
            for name in files:
                shutil.copyfile(root/name, Path(temp)/name)
            result = subprocess.run([sys.executable, str(Path(temp)/'research_mcp.py'), '--help'],
                                    cwd=temp, capture_output=True, timeout=10,
                                    env={**os.environ, 'PYTHONPATH': temp})
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_publish_only_verified_pair_with_portable_permissions(self):
        original = self.build()
        (self.output/'unrelated-secret.txt').write_text('SECRET')
        target = self.output.parent/'portable'
        report = publish_snapshot(self.output, target)
        self.assertEqual({p.name for p in target.iterdir()}, {'manifest.json', 'facts.sqlite'})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in target.iterdir()))
        self.assertEqual(report['snapshot_id'], original.manifest['snapshot_id'])
        self.assertEqual(ResearchSnapshot(target).query('SELECT count(*) FROM appeals')['rows'], [[1]])
        with self.assertRaises(FileExistsError):
            publish_snapshot(self.output, target)

    def test_exact_host_allows_server_ip_and_port_but_no_urls_or_wildcards(self):
        for value in ['research.example.org', 'server:8770', '192.0.2.1:8770', '[::1]:8770']:
            self.assertEqual(allowed_host(value), value)
        for value in ['example.org:*', '*.example.org', 'https://example.org', 'example.org/path', 'server:70000']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                allowed_host(value)

    def test_invalid_snapshot_fails_before_listening(self):
        self.build()
        database = self.output/'facts.sqlite'
        database.chmod(0o644)
        database.write_bytes(b'not a valid snapshot')
        script = str(Path(__file__).resolve().parents[1]/'research_mcp.py')
        result = subprocess.run([sys.executable, script, 'serve', '--snapshot', str(self.output),
                                 '--host', '0.0.0.0', '--transport', 'streamable-http'],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b'Unsupported or altered snapshot', result.stderr)


@unittest.skipUnless(fixtures.HAS_MCP, 'Install the research-mcp extra')
class HTTPTests(unittest.TestCase):
    setUp = fixtures.MCPTransportTests.setUp

    def test_http_server_proxy_hosts_body_limit_and_health(self):
        import httpx
        snapshot = ResearchSnapshot(self.snapshot)
        server = make_server(snapshot, host='0.0.0.0', allowed_hosts=['server:8770', 'research.example.org'])
        self.assertTrue(server.settings.transport_security.enable_dns_rebinding_protection)
        async def run():
            app = http_app(server, snapshot)
            async with server.session_manager.run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://server:8770') as client:
                    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                        'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
                    headers = {'Accept': 'application/json, text/event-stream'}
                    self.assertEqual((await client.post('/mcp', json=body, headers=headers)).status_code, 200)
                    self.assertEqual((await client.post('/mcp', json=body, headers={**headers, 'Host': 'research.example.org'})).status_code, 200)
                    self.assertEqual((await client.post('/mcp', json=body, headers={**headers, 'Host': 'evil.example.org'})).status_code, 421)
                    self.assertEqual((await client.post('/mcp', json=body, headers={**headers, 'Origin': 'https://evil.example.org'})).status_code, 403)
                    self.assertEqual((await client.post('/mcp', json=body, headers={**headers, 'Origin': 'http://server:8770'})).status_code, 200)
                    self.assertEqual((await client.post('/mcp', content=b'x'*65537, headers={**headers, 'Content-Type': 'application/json'})).status_code, 413)
                    self.assertEqual((await client.get('/healthz')).json()['snapshot_id'], snapshot.manifest['snapshot_id'])
        asyncio.run(run())
