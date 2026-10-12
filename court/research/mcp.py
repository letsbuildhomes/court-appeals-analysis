"""The research MCP server: bounded read-only SQL over one snapshot.

It reads only the snapshot directory (``facts.sqlite`` and ``manifest.json``),
never pipeline data or credentials. Over HTTP it checks Host and Origin
headers against loopback and explicitly allowed names.
"""
import json
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlsplit

from court.research.snapshot import ResearchSnapshot


def allowed_host(value):
    """One exact HTTP Host authority, including an optional port or IP address."""
    parsed = urlsplit('//'+value)
    if not re.fullmatch(r'[a-zA-Z0-9.\[\]:-]+', value) or not parsed.hostname or parsed.path:
        raise ValueError('allowed-host must be a hostname or IP with optional port, not a URL or wildcard')
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid allowed-host port') from None
    return value


def publish_snapshot(source, output):
    """Stage exactly the validated curated pair with portable read-only permissions."""
    snapshot = ResearchSnapshot(source)
    output = Path(output)
    output.mkdir(mode=0o755, parents=False)  # Never replace an existing version.
    try:
        (output/'facts.sqlite').write_bytes(snapshot.image)
        (output/'manifest.json').write_text(json.dumps(snapshot.manifest, ensure_ascii=False, indent=2)+'\n')
        for name in ('facts.sqlite', 'manifest.json'):
            (output/name).chmod(0o444)
        output.chmod(0o755)
        ResearchSnapshot(output)
    except BaseException:
        shutil.rmtree(output)
        raise
    return {'snapshot_id': snapshot.manifest['snapshot_id'], 'output': str(output.resolve())}


def make_server(snapshot, port=8770, host='127.0.0.1', allowed_hosts=()):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        raise ValueError('Install the optional SDK: uv sync --inexact --extra research-mcp') from None
    authorities = [allowed_host(value) for value in allowed_hosts]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=['127.0.0.1:*', 'localhost:*', '[::1]:*', *authorities],
        allowed_origins=['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*',
                         *[scheme+'://'+value for value in authorities for scheme in ('http', 'https')]])
    server = FastMCP('Municipal housing appeals research', host=host, port=port,
        json_response=True, stateless_http=True,
        max_request_body_size=65536, transport_security=security,
        instructions='Call describe_dataset before research. Query only structured extracted facts. '
        'Use appeals for in-scope cases, uncertain_appeals explicitly for uncertainty, cases for processing status. '
        'Do not invent appeal success, failed appeals, construction delay, abandonment or quantitative housing impacts. '
        'Preserve unknowns and exact versus minimum durations. Never classify source text.')
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    import anyio
    query_slots = anyio.CapacityLimiter(4)

    @server.tool(annotations=annotations)
    def describe_dataset() -> dict[str, Any]:
        """Discover pinned snapshot provenance, tables/columns, meanings, joins, SQL functions and research limitations."""
        return snapshot.describe()

    @server.tool(annotations=annotations)
    async def query_sql(sql: str, parameters: list | dict | None = None, max_rows: int = 200) -> dict[str, Any]:
        """Execute one bounded read-only SQLite SELECT, including joins/CTEs/windows. Prefer parameters for filter values.

        Read describe_dataset first. The appeals view has in-scope cases. Child joins multiply rows;
        use EXISTS for appellant filters. Duration at_least is a minimum; permission is not appellant success.
        Returns columns and rows arrays, provenance, actual SQL/parameters, truncation and limitations.
        """
        return await anyio.to_thread.run_sync(snapshot.query, sql, parameters, max_rows, limiter=query_slots)

    return server


def http_app(server, snapshot):
    """The SDK endpoint plus the identity of the validated pinned image."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    app = server.streamable_http_app()
    async def health(request):
        manifest = snapshot.manifest
        return JSONResponse({'status': 'ready', 'snapshot_id': manifest['snapshot_id'],
            'results': manifest['provenance']['results'], 'database_sha256': manifest['database_sha256'],
            'disposition_counts': manifest['disposition_counts']})
    app.routes.append(Route('/healthz', health))
    return app


def serve(snapshot_dir, host='127.0.0.1', port=8770, transport='stdio', allowed_hosts=()):
    snapshot = ResearchSnapshot(snapshot_dir)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    server = make_server(snapshot, port, host, allowed_hosts)
    if transport == 'stdio':
        server.run(transport='stdio')
        return
    import uvicorn
    uvicorn.run(http_app(server, snapshot), host=server.settings.host, port=port, proxy_headers=False,
                access_log=False, timeout_keep_alive=5, timeout_graceful_shutdown=10)


def main():
    """Serve one snapshot; the container entry point, which needs no other project code."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--transport', choices=('stdio', 'streamable-http'), default='stdio')
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--allowed-host', action='append', default=[])
    args = parser.parse_args()
    serve(args.snapshot, args.host, args.port, args.transport, args.allowed_host)


if __name__ == '__main__':
    main()
