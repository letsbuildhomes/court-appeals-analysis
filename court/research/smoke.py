"""Verify a remote research MCP using the official SDK; no model/client-account calls."""
import argparse
import asyncio
import json
from urllib.parse import urlsplit


def tool_data(result):
    if result.isError:
        raise ValueError('MCP tool returned an error')
    return result.structuredContent or json.loads(result.content[0].text)


async def smoke(url, expected_snapshot=None):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(timeout=20, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listing = await session.list_tools()
                if {t.name for t in listing.tools} != {'describe_dataset', 'query_sql'}:
                    raise ValueError('Unexpected tool inventory')
                if not all(t.annotations and t.annotations.readOnlyHint and t.outputSchema for t in listing.tools):
                    raise ValueError('Missing read-only annotations or output schemas')
                dataset = tool_data(await session.call_tool('describe_dataset', {}))
                snapshot_id = dataset['snapshot']['snapshot_id']
                if expected_snapshot and snapshot_id != expected_snapshot:
                    raise ValueError('Remote snapshot does not match the expected snapshot ID')
                query = tool_data(await session.call_tool('query_sql', {
                    'sql': 'SELECT disposition, count(*) AS cases FROM cases WHERE disposition IS NOT NULL '
                           'GROUP BY disposition ORDER BY disposition'}))
                if query['snapshot_id'] != snapshot_id or query['truncated']:
                    raise ValueError('Query provenance or completeness failed')
                counts = dict(query['rows'])
                if any(counts.get(k, 0) != v for k, v in dataset['snapshot']['disposition_counts'].items()):
                    raise ValueError('Query counts do not match snapshot metadata')
                for sql in ["SELECT readfile('/etc/passwd')", 'SELECT name FROM sqlite_master',
                            'SELECT description FROM appeals', 'SELECT "description" FROM appeals']:
                    rejected = await session.call_tool('query_sql', {'sql': sql})
                    if not rejected.isError:
                        raise ValueError('Read boundary test was unexpectedly accepted')
                recovered = tool_data(await session.call_tool('query_sql', {'sql': 'SELECT count(*) AS appeals FROM appeals'}))
                if recovered['snapshot_id'] != snapshot_id:
                    raise ValueError('Recovery query changed snapshots')
                return {'status': 'passed', 'server': initialized.serverInfo.name,
                    'protocol_version': initialized.protocolVersion, 'snapshot_id': snapshot_id,
                    'results': dataset['snapshot']['provenance']['results'], 'sqlite_version': dataset['sqlite_version'],
                    'disposition_counts': dataset['snapshot']['disposition_counts'],
                    'checks': ['initialize', 'discovery', 'structured query', 'provenance', 'denied file/schema/narrative reads', 'recovery']}


def check(url, expected_snapshot=None):
    address = urlsplit(url)
    if address.username or address.password or address.query or address.fragment or address.path != '/mcp':
        raise ValueError('Use an uncredentialed /mcp URL without query or fragment')
    if address.scheme not in ('http', 'https') or not address.hostname:
        raise ValueError('Use an HTTP or HTTPS endpoint')
    return asyncio.run(asyncio.wait_for(smoke(url, expected_snapshot), timeout=60))
