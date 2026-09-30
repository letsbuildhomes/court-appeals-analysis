"""Local structured appeal research: list runs, freeze a snapshot, query, or serve MCP."""
import argparse
import json
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlsplit

from research_snapshot import ResearchSnapshot, SAFE_NAME, build_snapshot

ROOT = Path(__file__).resolve().parent


def list_runs(root):
    """Metadata inventory only; never implicitly pick the newest run."""
    runs = []
    for path in Path(root).glob('*/manifest.json'):
        manifest = json.loads(path.read_bytes())
        statuses = {}
        for group in manifest.get('groups', []):
            gid = group['group_id']
            if not SAFE_NAME.fullmatch(gid):
                raise ValueError('Invalid group identity')
            checkpoint = path.parent/'results'/(gid+'.json')
            status = json.loads(checkpoint.read_bytes()).get('status', 'not_run') if checkpoint.exists() else 'not_run'
            statuses[status] = statuses.get(status, 0)+1
        runs.append({'run': path.parent.name, 'created_at': manifest.get('created_at'),
                     'groups': len(manifest.get('groups', [])), 'checkpoint_statuses': statuses,
                     'run_dir': str(path.parent.resolve())})
    return sorted(runs, key=lambda r: r.get('created_at') or '', reverse=True)


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
        'Use appeals for confirmed scope, uncertain_appeals explicitly for uncertainty, coverage for processing status. '
        'Do not invent appeal success, failed appeals, construction delay, abandonment or quantitative housing impacts. '
        'Preserve unknowns, review flags and exact versus minimum durations. Never classify source text.')
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

        Read describe_dataset first. Default appeals has confirmed scope. Child joins multiply rows;
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
            'run_name': manifest['run_name'], 'database_sha256': manifest['database_sha256'],
            'scope_counts': manifest['scope_counts']})
    app.routes.append(Route('/healthz', health))
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='List local runs; selection remains explicit')
    listing.add_argument('--root', type=Path, default=ROOT/'data/appeals')
    building = commands.add_parser('build', help='Freeze finalized structured checkpoints; refuse overwrite')
    selection = building.add_mutually_exclusive_group(required=True)
    selection.add_argument('--run', help='Run directory name under --root')
    selection.add_argument('--run-dir', type=Path, help='Explicit local run directory, including transferred runs')
    building.add_argument('--root', type=Path, default=ROOT/'data/appeals')
    building.add_argument('--output', type=Path, required=True, help='New immutable snapshot directory')
    for name in ('describe', 'query', 'serve', 'client-config', 'publish'):
        command = commands.add_parser(name)
        command.add_argument('--snapshot', type=Path, required=True)
        if name == 'publish':
            command.add_argument('--output', type=Path, required=True, help='New two-file portable snapshot directory')
        if name == 'query':
            command.add_argument('--sql', required=True)
            command.add_argument('--parameters', default='[]', help='JSON array/object of bound SQL values')
            command.add_argument('--max-rows', type=int, default=200)
        if name == 'serve':
            command.add_argument('--transport', choices=('stdio', 'streamable-http'), default='stdio')
            command.add_argument('--port', type=int, default=8770, help='HTTP loopback port; unused for stdio')
            command.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1', help='HTTP bind address')
            command.add_argument('--allowed-host', action='append', default=[], help='Additional exact Host, e.g. server:8770 or research.example.org')
    args = parser.parse_args()
    try:
        if args.command == 'list':
            result = list_runs(args.root)
        elif args.command == 'build':
            if args.run and not SAFE_NAME.fullmatch(args.run):
                raise ValueError('Invalid run name')
            result = build_snapshot(args.run_dir or args.root/args.run, args.output)
        else:
            if args.command == 'publish':
                print(json.dumps(publish_snapshot(args.snapshot, args.output), indent=2))
                return
            snapshot = ResearchSnapshot(args.snapshot)
            if args.command == 'client-config':
                # Print a mergeable snippet; never write the user's client config.
                interpreter = ROOT/'.venv/bin/python'
                if not interpreter.exists():
                    raise ValueError('Run make research-setup first')
                result = {'mcpServers': {'housing-appeals': {'command': str(interpreter),
                    'args': [str(ROOT/'research_mcp.py'), 'serve', '--snapshot', str(args.snapshot.resolve())]}}}
                print(json.dumps(result, indent=2))
                return
            if args.command == 'serve':
                if not 1 <= args.port <= 65535:
                    raise ValueError('Invalid localhost port')
                if args.transport == 'stdio' and (args.host != '127.0.0.1' or args.allowed_host):
                    raise ValueError('--host and --allowed-host require --transport streamable-http')
                server = make_server(snapshot, args.port, args.host, args.allowed_host)
                if args.transport == 'stdio':
                    server.run(transport='stdio')
                else:
                    import uvicorn
                    uvicorn.run(http_app(server, snapshot), host=server.settings.host,
                        port=args.port, proxy_headers=False, access_log=False,
                        timeout_keep_alive=5, timeout_graceful_shutdown=10)
                return
            result = snapshot.describe() if args.command == 'describe' else snapshot.query(
                args.sql, json.loads(args.parameters), args.max_rows)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
