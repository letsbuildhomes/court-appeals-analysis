"""The command line: ``python -m court COMMAND``.

Each command parses its arguments and calls one function. Only commands
given ``--send`` make paid model calls; they read credentials from the
environment (``make`` passes ``.env`` only to those targets).
"""
import argparse
import json
from pathlib import Path
import sys

from court.core.datasets import Datasets, diff as dataset_diff
from court.core.pipelines import Pipelines, diff as pipeline_diff, walk
from court.core.runner import Runner


def show(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def pairs(values):
    found = {}
    for value in values or []:
        name, sep, ref = value.partition('=')
        if not sep:
            raise SystemExit(f'Inputs are NAME=DATASET, got {value!r}')
        found[name] = ref
    return found


def key_list(args):
    keys = []
    for value in args.keys or []:
        keys += [k for k in value.split(',') if k]
    if args.keys_file:
        keys += [line.strip() for line in open(args.keys_file) if line.strip()]
    return keys or None


def mode(args):
    if args.send and args.dry:
        raise SystemExit('Choose --send or --dry, not both')
    if args.send and args.max_spend is None:
        raise SystemExit('--send needs --max-spend, the most this run may newly spend in USD')
    return 'send' if args.send else 'dry' if args.dry else 'cached'


# Commands -------------------------------------------------------------------
def run(args):
    record = Runner().run(args.name, args.pipeline, pairs(args.input), at=args.at or '', keys=key_list(args),
                          mode=mode(args), limit=args.max_spend or 0.0, resend_failed=args.resend_failed,
                          resend_max_tokens=args.resend_max_tokens, resamples=args.resamples,
                          workers=args.workers, save_all=args.save_all, note=args.note or '')
    show({k: record[k] for k in ('name', 'pipeline', 'at', 'status', 'output', 'outputs', 'items', 'cost')})


def runs(args):
    for record in Runner().all():
        print(f'{record["name"]:40} {record["pipeline"]} {record["at"] or "(root)":12} {record["status"]:9} '
              f'{record["items"]["total"]:5} items  {record["output"]}')


def pipelines(args):
    store = Pipelines()
    tags = {}
    for tag, entry in store.tags().items():
        tags.setdefault(entry['pipeline'], []).append(tag)
    for resolved in store.all():
        print(f'{resolved["id"]}  {resolved.get("name") or "":24} {", ".join(tags.get(resolved["id"], []))}')


def pipeline(args):
    store = Pipelines()
    if args.action == 'resolve':
        resolved = store.resolve(store.definition(args.ref))
        print(resolved['id'])
    elif args.action == 'show':
        resolved = store.load(args.ref)
        print(f'{resolved["id"]}  {resolved.get("name") or ""}\n{resolved.get("description") or ""}\n')
        for path, node in walk(resolved['root']):
            depth = path.count('/') + (1 if path else 0)
            extra = {k: node[k] for k in ('settings', 'prompts', 'save') if node.get(k)}
            print('  '*depth + f'{node["name"]}  {node["step"]}' + (f'  {json.dumps(extra)}' if extra else ''))
    elif args.action == 'diff':
        show(pipeline_diff(store.load(args.ref), store.load(args.other)))


def tag(args):
    show(Pipelines().set_tag(args.tag, Pipelines().load(args.pipeline)['id'], note=args.note or ''))


def tags(args):
    for entry in Pipelines().tag_log():
        print(f'{entry["at"][:19]}  {entry["tag"]:12} {entry["pipeline"]}  {entry.get("note", "")[:80]}')


def datasets(args):
    store = Datasets()
    names = {}
    for name, identity in store.names().items():
        names.setdefault(identity, []).append(name)
    for dataset in store.all():
        if dataset.kind == 'traces' and not args.all:
            continue
        print(f'{dataset.id}  {dataset.kind:10} {len(dataset):6} items  {", ".join(names.get(dataset.id, []))}')


def dataset(args):
    store = Datasets()
    if args.action == 'show':
        found = store.open(args.ref)
        if args.key:
            value = found.get(args.key)
            show(value.to_json() if hasattr(value, 'to_json') else value)
        else:
            show({k: v for k, v in found.manifest.items() if k != 'items'} | {'items': len(found)})
    elif args.action == 'diff':
        counts = dataset_diff(store.open(args.ref), store.open(args.other))
        show({k: len(v) for k, v in counts.items()} | {'changed_keys': counts['changed'][:50]})
    elif args.action == 'name':
        store.name(args.name, store.open(args.ref).id, note=args.note or '')
    elif args.action == 'retire':
        store.retire(args.ref, note=args.note or '')


def lab(args):
    from court.lab.experiments import Lab, Regressions
    lab = Lab()
    if args.action == 'new':
        changes = json.loads(args.changes) if args.changes else []
        probe = key_list(args) or []
        show(lab.new(args.name, args.hypothesis, baseline=args.baseline, start=args.at or '',
                     inputs=pairs(args.input), probe='changed' if probe == ['changed'] else probe,
                     confirm=args.confirm and (args.confirm if args.confirm in ('reference', 'all')
                                               else args.confirm.split(',')),
                     reference=args.reference, changes=changes, focus=args.focus))
    elif args.action == 'plan':
        show(lab.plan(args.name, args.round))
    elif args.action == 'run':
        result = lab.run(args.name, args.round, mode=mode(args), limit=args.max_spend or 0.0,
                         resamples=args.resamples, resend_failed=args.resend_failed,
                         resend_max_tokens=args.resend_max_tokens, workers=args.workers)
        show({k: result[k] for k in ('round', 'status', 'regressions', 'accuracy', 'cost')}
             | {'headline': result['summary']['headline'], 'verdicts': result['summary']['verdicts']})
    elif args.action == 'baseline':
        record = lab.baseline(args.name, keys=key_list(args), mode=mode(args), limit=args.max_spend or 0.0,
                              resend_failed=args.resend_failed, resend_max_tokens=args.resend_max_tokens,
                              workers=args.workers)
        show({k: record[k] for k in ('name', 'pipeline', 'status', 'output', 'items', 'cost')})
    elif args.action == 'adopt':
        try:
            entry = lab.adopt(args.name, args.tag, note=args.note or '', accept_regressions=args.accept_regressions)
        except Regressions as worse:
            print(f'{worse}:', file=sys.stderr)
            for case in worse.cases:
                print(f'  {case["case_id"]}: {"; ".join(case["chips"])}', file=sys.stderr)
            if not sys.stdin.isatty():
                raise SystemExit('Pass --accept-regressions to adopt anyway')
            if input(f'Adopt anyway? The {args.tag} tag log records these cases. [y/N] ').strip().lower() != 'y':
                raise SystemExit('Not adopted')
            entry = lab.adopt(args.name, args.tag, note=args.note or '', accept_regressions=True)
        show(entry)
    elif args.action == 'list':
        for record in lab.all():
            rounds = lab.rounds(record['name'])
            print(f'{record["name"]:30} {", ".join(f"{r}: {v["summary"]["headline"]}" for r, v in rounds.items())}')


def web(args):
    import os
    from court.config import ROOT
    from court.ui.server import serve
    from court.web import Password, PublicUrl
    secret = os.getenv('COURT_WEB_PASSWORD')
    path = Path(args.password_file) if args.password_file else ROOT/'.web-password'
    if not secret and path.exists():
        secret = path.read_text().strip()
    if not secret and not args.no_password:
        raise SystemExit('Set a password: COURT_WEB_PASSWORD, or one line in .web-password (or --password-file); '
                         '--no-password serves the app to anyone who can reach it')
    url = args.public_url or os.getenv('COURT_WEB_PUBLIC_URL')
    try:
        public = PublicUrl(url) if url else None
    except ValueError as exc:
        raise SystemExit(str(exc))
    serve(args.host, args.port, args.allowed_host or (), Password(secret) if secret else None, public)


def reports(args):
    from court.reports import cases, text
    if args.report == 'grouping':
        show(cases.report(Datasets().open(args.ref).load()))
    elif args.report == 'case-diff':
        show(cases.diff(Datasets().open(args.ref).load(), Datasets().open(args.other).load()))
    elif args.report == 'text-audit':
        show(text.audit(Datasets().open(args.ref).load()))


def research(args):
    if args.action == 'build':
        from court.research import build
        show(build.build(args.results, args.cases, args.output))
    elif args.action == 'describe':
        from court.research.snapshot import ResearchSnapshot
        show(ResearchSnapshot(args.snapshot).describe())
    elif args.action == 'query':
        from court.research.snapshot import ResearchSnapshot
        show(ResearchSnapshot(args.snapshot).query(args.sql, json.loads(args.parameters or '[]'), args.max_rows))
    elif args.action == 'serve':
        from court.research import mcp
        mcp.serve(args.snapshot, args.host, args.port, args.transport, args.allowed_host or ())
    elif args.action == 'publish':
        from court.research import mcp
        show(mcp.publish_snapshot(args.snapshot, args.output))
    elif args.action == 'smoke':
        from court.research import smoke
        show(smoke.check(args.url, args.expected_snapshot))


def reference(args):
    from court.reference.workspace import Workspace
    space = Workspace()
    if args.action == 'freeze':
        show(space.freeze(args.definition))
    elif args.action == 'pools':
        show(space.pools())
    elif args.action == 'check' and args.case:
        problems = space.check_label(args.case)
        show({'case': args.case, 'problems': problems})
        if problems:
            raise SystemExit(1)
    elif args.action == 'check':
        found = space.check()
        show(found)
        if found['problems']:
            raise SystemExit(1)
    elif args.action == 'export':
        show(space.export(args.name, args.split))


def import_legacy(args):
    from court.migrate import legacy
    legacy.run()
    legacy.seed_downloads()
    legacy.seed_text()


# Arguments --------------------------------------------------------------------
def sending(parser):
    parser.add_argument('--send', action='store_true', help='Send new model calls (paid); needs --max-spend')
    parser.add_argument('--dry', action='store_true', help='Estimate new calls without sending')
    parser.add_argument('--max-spend', type=float,
                        help='Most this command may spend on new calls, in USD; earlier executions of the run don\'t count')
    parser.add_argument('--resend-failed', action='store_true', help='Resend each failed delivery once more')
    parser.add_argument('--resend-max-tokens', type=int, help='With --resend-failed, the output limit for resent calls')
    parser.add_argument('--resamples', type=int, default=0, help='Further samples to try when an answer is rejected')
    parser.add_argument('--workers', type=int, default=16, help='Calls in flight at once, per step')


def keyed(parser):
    parser.add_argument('--keys', action='append', help='Comma-separated keys to limit to')
    parser.add_argument('--keys-file', help='A file of keys, one per line')


def parser():
    top = argparse.ArgumentParser(prog='court', description=__doc__.splitlines()[0])
    sub = top.add_subparsers(dest='command', required=True)

    p = sub.add_parser('run', help='Run a pipeline, or the subtree at a position, on datasets')
    p.add_argument('name', help='Run name; running a name again continues it')
    p.add_argument('pipeline', help='Pipeline ID or tag')
    p.add_argument('--at', help='Position to start at, e.g. extract')
    p.add_argument('--input', action='append', help='NAME=DATASET for the starting step')
    p.add_argument('--save-all', action='store_true', help='Save every step\'s output')
    p.add_argument('--note')
    keyed(p)
    sending(p)
    p.set_defaults(func=run)

    sub.add_parser('runs', help='List runs').set_defaults(func=runs)
    p = sub.add_parser('rename-run', help='Rename a run; its old name still finds it')
    p.add_argument('old')
    p.add_argument('new')
    p.set_defaults(func=lambda args: show(Runner().rename(args.old, args.new)['name']))
    sub.add_parser('pipelines', help='List resolved pipelines and their tags').set_defaults(func=pipelines)

    p = sub.add_parser('pipeline', help='Resolve, show or diff pipelines')
    p.add_argument('action', choices=('resolve', 'show', 'diff'))
    p.add_argument('ref', help='A definition name (resolve) or a pipeline ID or tag')
    p.add_argument('other', nargs='?')
    p.set_defaults(func=pipeline)

    p = sub.add_parser('tag', help='Point a tag at a pipeline')
    p.add_argument('tag')
    p.add_argument('pipeline')
    p.add_argument('--note')
    p.set_defaults(func=tag)
    sub.add_parser('tags', help='Show the tag log').set_defaults(func=tags)

    p = sub.add_parser('datasets', help='List datasets')
    p.add_argument('--all', action='store_true', help='Include traces')
    p.set_defaults(func=datasets)
    p = sub.add_parser('dataset', help='Show, diff, name datasets, or retire a name')
    p.add_argument('action', choices=('show', 'diff', 'name', 'retire'))
    p.add_argument('ref')
    p.add_argument('other', nargs='?')
    p.add_argument('--key')
    p.add_argument('--name')
    p.add_argument('--note')
    p.set_defaults(func=dataset)

    p = sub.add_parser('lab', help='Experiments')
    p.add_argument('action', choices=('new', 'plan', 'run', 'baseline', 'adopt', 'list'))
    p.add_argument('name', nargs='?')
    p.add_argument('--hypothesis')
    p.add_argument('--baseline', default='production')
    p.add_argument('--at', help='Position the experiment\'s runs start at')
    p.add_argument('--focus', help='Position the keys apply to, when later than --at; steps before it run on everything')
    p.add_argument('--input', action='append', help='NAME=DATASET for the starting step')
    p.add_argument('--confirm', help='Comma-separated confirm keys, "reference" or "all"')
    p.add_argument('--reference', help='Reference dataset')
    p.add_argument('--changes', help='JSON list of changes to the baseline')
    p.add_argument('--round', default='probe', choices=('probe', 'confirm'))
    p.add_argument('--tag', default='production')
    p.add_argument('--note')
    p.add_argument('--accept-regressions', action='store_true')
    keyed(p)
    sending(p)
    p.set_defaults(func=lab)

    p = sub.add_parser('reference', help='The reference set: freeze its cases, build candidate pools, check, export')
    p.add_argument('action', choices=('freeze', 'pools', 'check', 'export'))
    p.add_argument('--definition', default='docket-cases', help='Pipeline definition whose grouping to freeze')
    p.add_argument('--name', default='reference', help='Dataset name to export under')
    p.add_argument('--split', choices=('development', 'holdout'), help='Export only one split')
    p.add_argument('--case', help='Check one case\'s label file only')
    p.set_defaults(func=reference)

    p = sub.add_parser('web', help='Serve the web app: live runs, the lab, data viewers and pipelines (read-only)')
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8771)
    p.add_argument('--allowed-host', action='append', help='A hostname to accept besides IP addresses and localhost')
    p.add_argument('--password-file', help='A file whose first line is the password (default .web-password)')
    p.add_argument('--no-password', action='store_true', help='Serve without a password')
    p.add_argument('--public-url', help='The address a reverse proxy serves the app at, e.g. https://reviews.example.org/app/ '
                                        '(default COURT_WEB_PUBLIC_URL)')
    p.set_defaults(func=web)

    p = sub.add_parser('report', help='Reports on saved datasets')
    p.add_argument('report', choices=('grouping', 'case-diff', 'text-audit'))
    p.add_argument('ref')
    p.add_argument('other', nargs='?')
    p.set_defaults(func=reports)

    p = sub.add_parser('research', help='Research snapshots and the MCP server')
    p.add_argument('action', choices=('build', 'describe', 'query', 'serve', 'publish', 'smoke'))
    p.add_argument('--url', help='A remote /mcp endpoint to check (smoke)')
    p.add_argument('--expected-snapshot', help='The snapshot ID the remote server must serve (smoke)')
    p.add_argument('--results', help='Results dataset to freeze (build)')
    p.add_argument('--cases', help='Its case set, when the results do not record it (build)')
    p.add_argument('--output', help='New snapshot directory (build: default data/research/<results ID>; publish)')
    p.add_argument('--snapshot', help='Snapshot directory (describe, query, serve)')
    p.add_argument('--sql')
    p.add_argument('--parameters')
    p.add_argument('--max-rows', type=int, default=200)
    p.add_argument('--host', default='127.0.0.1', choices=('127.0.0.1', '0.0.0.0'))
    p.add_argument('--port', type=int, default=8770)
    p.add_argument('--transport', default='stdio', choices=('stdio', 'streamable-http'))
    p.add_argument('--allowed-host', action='append', help='Additional exact Host for HTTP, e.g. server:8770')
    p.set_defaults(func=research)

    sub.add_parser('import-legacy', help='Wrap the legacy pipeline\'s September 2026 data as datasets and seed the caches'
                   ).set_defaults(func=import_legacy)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.func(args)
    except (KeyError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0
