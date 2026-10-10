"""Read-only dashboard for frozen appeal experiment artifacts.

It listens on the private network by default. Host checks admit loopback, IP
literals, and explicitly allowed names, which keeps DNS rebinding out.
"""
import argparse
from collections import Counter
import hashlib
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlsplit

from appeal_demo import build_demo
from appeal_evaluation import evaluate
from appeal_impact import CASE_FACTS, RECORD_FACTS, compare_case, impact, processing, profile
from appeal_reference import read_reference
from pipeline import registry, runs
from listen import DEFAULT_HOST, display_url

ROOT = Path(__file__).resolve().parent
BASELINE_DESCRIPTION = ('The first production run, rebuilt by the ported legacy pipeline from its saved calls. Its '
                        'quality profile shows where answers are missing, which is where experiments can look for '
                        'improvements.')
VERDICTS = ('worsened', 'mixed', 'changed', 'improved', 'unchanged')
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
EXPERIMENT_VERSION = 'appeal-experiment-3'


def variables(record, rows):
    """Short descriptions of what an experiment changed."""
    result = registry.summary(rows) if rows is not None else []
    if (record or {}).get('samples'):
        result += [f'{path}: sample {n}' for path, n in record['samples'].items()]
    if ((record or {}).get('batching') or {}).get('status') == 'changed':
        result.append('batching')
    retrospective = (record or {}).get('kind') == 'retrospective'
    return result or ['Same pipeline' if retrospective else 'No changes: identical rerun']


def signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def tracked(folder):
    """Files whose change means a run must be read again."""
    folder = Path(folder)
    return [folder/'run.json'] + [p for p in (folder/'executions.json', folder/'ledger.jsonl') if p.exists()]


def pipelines(record):
    """Both sides' descriptors and their step-by-step diff."""
    designs = {}
    for side in ('baseline', 'candidate'):
        try:
            designs[side] = registry.load(record[side]['pipeline'])
        except (KeyError, ValueError, TypeError):
            designs[side] = None
    descriptors = {side: (d or {}).get('descriptor') for side, d in designs.items()}
    rows = registry.diff(descriptors['baseline'], descriptors['candidate']) \
        if descriptors['baseline'] and descriptors['candidate'] else None
    return {'baseline': designs['baseline'], 'candidate': designs['candidate'], 'diff': rows}


class Dashboard:
    def __init__(self, production=None, baseline=None, candidate=None, reference=None, experiments=None):
        self.production = Path(production or runs.RUNS/'production')
        self.experiments = Path(experiments or ROOT/'data/experiments')
        self.configured = [Path(p) if p else None for p in (baseline, candidate, reference)]
        if candidate and not baseline or reference and not candidate:
            raise ValueError('A candidate requires a baseline; a reference requires both runs')
        self.cache = {}
        self.lock = threading.RLock()

    def experiment_dirs(self):
        """Experiment records in the current format; earlier records stay on disk, unlisted."""
        if not self.experiments.is_dir():
            return []
        found = []
        for p in sorted(self.experiments.iterdir()):
            if p.is_dir() and not p.name.startswith('.') and (p/'experiment.json').is_file():
                try:
                    current = json.loads((p/'experiment.json').read_text()).get('schema_version') == EXPERIMENT_VERSION
                except (OSError, json.JSONDecodeError):
                    current = True  # Listed, so the error is visible.
                if current:
                    found.append(p)
        return found

    def experiment_folder(self, dataset):
        name = dataset.partition(':')[2]
        folder = self.experiments/name
        if not SAFE_NAME.fullmatch(name) or not (folder/'experiment.json').is_file():
            raise ValueError('Unknown dataset')
        return folder

    def catalog(self):
        datasets = []
        for folder in self.experiment_dirs():
            try:
                datasets.append(self.card('experiment:'+folder.name))
            except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
                datasets.append({'id': 'experiment:'+folder.name, 'label': folder.name, 'kind': 'experiment',
                                 'available': False, 'error': str(exc)})
        datasets.sort(key=lambda d: d.get('created_at') or '', reverse=True)
        if self.configured[0]:
            datasets.append(self.card('configured'))
        datasets.append(self.card('demo'))
        datasets.append({'id': 'production', 'label': 'Production baseline', 'kind': 'baseline',
                         'available': (self.production/'run.json').is_file(), 'description': BASELINE_DESCRIPTION})
        return {'datasets': datasets, 'default': None, 'tags': registry.tags(),
                'history': {tag: registry.history(tag) for tag in registry.tags()}}

    def card(self, dataset):
        """Index summary: hypothesis, variables, status, impact and cost."""
        value = self.load(dataset)
        record = value.get('experiment') or {}
        result = value.get('impact')
        selected = (result or {}).get('groups', {}).get('selected')
        rows = (value.get('pipelines') or {}).get('diff')
        return {'id': dataset, 'label': record.get('name') or {'demo': 'Synthetic software test',
                'configured': 'Supplied comparison', 'production': 'Production baseline'}.get(dataset, dataset),
                'kind': value['kind'], 'available': True, 'status': value['status'],
                'hypothesis': record.get('hypothesis') or value.get('description'),
                'created_at': record.get('created_at'),
                'variables': variables(record, rows) if record or rows else [],
                'cohort': (record.get('cohort') or {}).get('name'),
                'pipelines': {side: (record.get(side) or {}).get('pipeline') for side in ('baseline', 'candidate')}
                if record else None,
                'cases': {'selected': len((record.get('cases') or {}).get('selected', [])) or (selected or {}).get('cases'),
                          'companions': len((record.get('cases') or {}).get('companions', []))},
                'impact': {k: selected[k] for k in ('verdicts', 'headline', 'flows', 'cases')} if selected else None,
                'reference': bool(result and result.get('reference')),
                'cost': value.get('cost_summary'), 'progress': value.get('progress')}

    def load(self, dataset):
        with self.lock:
            if dataset == 'demo':
                if dataset not in self.cache:
                    demo = build_demo()
                    demo.update(kind='synthetic', status='finalized', description='Invented cases with reference '
                                'labels that exercise every comparison outcome. Not research results.')
                    demo['impact'] = impact(demo['baseline'], demo['candidate'], demo['evaluation'], demo['reference'])
                    demo['cost_summary'] = {'new_usd': None, 'simulated': True}
                    self.cache[dataset] = (None, demo)
                return self.cache[dataset][1]
            record, roles, kind, status, cases, folder = None, {}, 'comparison', 'finalized', None, None
            if dataset == 'production':
                paths = [self.production, None, None]
                kind = 'baseline'
            elif dataset == 'configured' and self.configured[0]:
                paths = self.configured
                kind = 'comparison' if self.configured[1] else 'baseline'
            elif dataset.startswith('experiment:'):
                folder = self.experiment_folder(dataset)
                record = json.loads((folder/'experiment.json').read_text())
                if record.get('schema_version') != EXPERIMENT_VERSION:
                    raise ValueError('Unsupported experiment record; plan it again')
                roles = {gid: 'companion' for gid in record['cases']['companions']}
                kind = 'retrospective' if record.get('kind') == 'retrospective' else 'experiment'
                if not (folder/'comparison.json').is_file():
                    return self.planned(dataset, folder, record)
                paths = [Path(record['baseline']['run']), Path(record['candidate']['run']), None]
                cases = record['cases']['selected']+record['cases']['companions']
            else:
                raise ValueError('Unknown dataset')
            files = [f for p in paths[:2] if p for f in tracked(p)]
            if paths[2]:
                files.append(paths[2])
            if folder is not None:
                files += [folder/'experiment.json', folder/'comparison.json']
            before = tuple(signature(p) for p in files)
            if dataset in self.cache and self.cache[dataset][0] == before:
                return self.cache[dataset][1]
            loaded = [runs.load(p, cases) if p else None for p in paths[:2]]
            reference = read_reference(paths[2]) if paths[2] else None
            evaluation = evaluate(*loaded, reference) if loaded[1] else None
            value = {'baseline': loaded[0], 'candidate': loaded[1], 'reference': reference, 'evaluation': evaluation,
                     'kind': kind, 'status': status, 'experiment': record,
                     'roots': {side: Path(run['run']['corpus']).resolve()
                               for side, run in zip(('baseline', 'candidate'), loaded) if run},
                     'runs': {side: run['run'] for side, run in zip(('baseline', 'candidate'), loaded) if run}}
            if record is not None:
                value['pipelines'] = pipelines(record)
                value['comparison'] = json.loads((folder/'comparison.json').read_text())
                executions = Path(record['candidate']['run'])/'executions.json'
                value['executions'] = json.loads(executions.read_text()) \
                    if record['kind'] == 'experiment' and executions.exists() else []
            elif loaded[1]:
                value['pipelines'] = pipelines({side: {'pipeline': run['run']['pipeline']}
                                                for side, run in zip(('baseline', 'candidate'), loaded)})
            else:
                value['pipelines'] = pipelines({'baseline': {'pipeline': loaded[0]['run']['pipeline']},
                                                'candidate': {'pipeline': None}})
            if loaded[1]:
                value['impact'] = impact(value['baseline'], value['candidate'], evaluation, reference, roles)
                cost = (value.get('comparison') or {}).get('cost') or runs.cost(paths[1])
                value['cost_summary'] = {'new_usd': cost.get('new_usd'), 'reused_usd': cost.get('reused_usd'),
                                         'recorded_usd': cost.get('recorded_usd'),
                                         'retrospective': kind == 'retrospective'}
            else:
                value['profile'] = profile(value['baseline']['cases'])
                value['description'] = BASELINE_DESCRIPTION
                value['cost_summary'] = runs.cost(paths[0])
                try:
                    value['artifact_path'] = str(paths[0].resolve().relative_to(ROOT))
                except ValueError:
                    value['artifact_path'] = str(paths[0].resolve())
            if before != tuple(signature(p) for p in files):
                raise ValueError('Artifacts changed during read')
            self.cache[dataset] = (before, value)
            return value

    def planned(self, dataset, folder, record):
        """An experiment that has not been finalized: plan, progress and spending only."""
        run_dir = Path(record['candidate']['run'])
        statuses = runs.progress(run_dir)['statuses']
        executions = json.loads((run_dir/'executions.json').read_text())
        cost = runs.cost(run_dir)
        return {'kind': 'experiment', 'status': 'running' if executions else 'planned', 'experiment': record,
                'pipelines': pipelines(record), 'executions': executions,
                'baseline': None, 'candidate': None, 'evaluation': None, 'reference': None,
                'progress': {'statuses': dict(statuses), 'cases': sum(statuses.values()),
                             'complete': statuses.get('complete', 0)},
                'cost_summary': {'new_usd': cost['new_usd'], 'reused_usd': cost['reused_usd'],
                                 'estimate': record.get('estimate')}}

    def overview(self, dataset):
        value = self.load(dataset)
        sides = []
        for side in ('baseline', 'candidate'):
            run = value.get(side)
            if run:
                sides.append({'side': side, 'name': run['run']['name'], 'pipeline': run['run']['pipeline'],
                              'kind': run['run']['kind'],
                              'status_counts': dict(Counter(c['status']['state'] for c in run['cases'])),
                              'case_count': len(run['cases']),
                              'record_count': sum(len(c.get('records') or []) for c in run['cases'])})
        result = value.get('impact')
        designs = value.get('pipelines') or {}
        return {'dataset': dataset, 'synthetic': dataset == 'demo',
                'kind': value['kind'], 'status': value['status'], 'card': self.card(dataset),
                'experiment': value.get('experiment'), 'executions': value.get('executions', []),
                'progress': value.get('progress'), 'cost_summary': value.get('cost_summary'),
                'impact': {k: v for k, v in result.items() if k != 'cases'} if result else None,
                'profile': value.get('profile'), 'artifact_path': value.get('artifact_path'),
                'runs': sides,
                'pipelines': {'baseline': (designs.get('baseline') or {}).get('descriptor'),
                              'candidate': (designs.get('candidate') or {}).get('descriptor'),
                              'ids': {side: (designs.get(side) or {}).get('id') for side in ('baseline', 'candidate')},
                              'diff': designs.get('diff')},
                'tags': registry.tags(),
                'fact_labels': {f['key']: f['label'] for f in CASE_FACTS + RECORD_FACTS},
                'reference': {k: v for k, v in value['reference'].items() if k != 'cases'} if value.get('reference') else None}

    def detail(self, dataset, case_id):
        value = self.load(dataset)
        result = {'case_id': case_id}
        for key in ('baseline', 'candidate', 'reference', 'evaluation'):
            result[key] = next((c for c in (value.get(key) or {}).get('cases', []) if c['case_id'] == case_id), None)
        if not any(result[k] for k in ('baseline', 'candidate', 'reference')):
            raise KeyError('Unknown case')
        result['impact'] = next((c for c in (value.get('impact') or {}).get('cases', []) if c['case_id'] == case_id), None)
        if result['impact'] is None and result['baseline']:
            # Baseline-only views list each record's answered and missing facts.
            result['facts'] = compare_case(result['baseline'], None)['one_sided']
        return result

    def cases(self, dataset, q='', status='', scope='', verdict='', role='', gap='', offset='0', limit='50'):
        """Filtered case rows, worst verdicts first when there is a comparison."""
        offset, limit = int(offset), int(limit)
        if offset < 0 or not 1 <= limit <= 200 or len(q) > 200:
            raise ValueError('Invalid pagination or search')
        if verdict and verdict not in VERDICTS or role and role not in ('selected', 'companion') \
                or gap and gap != 'duration_unknown':
            raise ValueError('Unknown filter')
        value = self.load(dataset)
        if value.get('baseline') is None:
            raise ValueError('This experiment has not been finalized')
        sides = {key: {c['case_id']: c for c in (value.get(key) or {}).get('cases', [])} for key in ('baseline', 'candidate')}
        impacts = {c['case_id']: c for c in (value.get('impact') or {}).get('cases', [])}
        rows = []
        for case_id in sorted(set(sides['baseline']) | set(sides['candidate'])):
            b, c = sides['baseline'].get(case_id), sides['candidate'].get(case_id)
            chosen, effect = b or c, impacts.get(case_id)
            statuses = {side: run['status']['state'] if run else None for side, run in (('baseline', b), ('candidate', c))}
            dockets = sorted({str(d) for run in (b, c) if run
                              for d in run['source'].get('dockets', []) + [r.get('docket') for r in run.get('records') or []]
                              if d and d != 'unknown'})
            unknown = sum(isinstance(r.get('duration'), dict) and r['duration'].get('kind') == 'unknown'
                          for r in chosen.get('records') or [])
            disposition = (chosen.get('disposition') or {}).get('value') or 'none'
            if q.casefold() not in ' '.join([case_id] + dockets).casefold():
                continue
            if status and not (any(processing(s) == status for s in statuses.values() if s)
                               or status in statuses.values()):
                continue
            if scope and scope != disposition or gap and not unknown:
                continue
            if verdict and (not effect or effect['verdict'] != verdict) or role and (not effect or effect['role'] != role):
                continue
            rows.append({'case_id': case_id, 'dockets': dockets, 'disposition': disposition,
                         'status': statuses, 'gaps': {'duration_unknown': unknown},
                         'record_count': {side: len(run.get('records') or []) if run else None
                                          for side, run in (('baseline', b), ('candidate', c))},
                         'impact': {k: effect[k] for k in ('verdict', 'verified', 'chips', 'highlights', 'role', 'processing')}
                         if effect else None})
        if impacts:
            # Attention first: worse, mixed, judgment, better, unchanged; cohort before companions.
            rows.sort(key=lambda r: (VERDICTS.index(r['impact']['verdict']), r['impact']['role'] != 'selected'))
        return {'cases': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit}

    def source(self, dataset, case_id, side='baseline'):
        if side not in ('baseline', 'candidate'):
            raise ValueError('Unknown side')
        value = self.load(dataset)
        case = self.detail(dataset, case_id)[side]
        if case is None:
            raise KeyError('Case absent from selected run')
        expected = case.get('source', {}).get('sha256')
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdefABCDEF' for c in expected):
            raise ValueError('Source hash unavailable; text withheld')
        if dataset == 'demo':
            raw = value['sources'][case_id].encode('utf-8')
        else:
            root = value['roots'][side]/'sources'
            path = root/f'{case_id}.txt'
            if not SAFE_NAME.fullmatch(case_id) or Path(case['source']['path']).resolve() != path.resolve():
                raise ValueError('Unsafe source path')
            if path.is_symlink():
                raise ValueError('Source symlinks are forbidden')
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root.resolve()):
                raise ValueError('Source outside approved root')
            before = signature(resolved)
            raw = resolved.read_bytes()
            if before != signature(resolved):
                raise ValueError('Source changed during read')
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected.lower():
            raise ValueError('Source hash mismatch; text withheld')
        return {'case_id': case_id, 'side': side, 'text': raw.decode('utf-8'), 'sha256': digest, 'verified': True}


def trusted_hostname(hostname, allowed_hosts=()):
    """DNS rebinding needs a name, so IP literals are safe; names must be listed."""
    if hostname == 'localhost' or hostname in allowed_hosts:
        return True
    try:
        ipaddress.ip_address(hostname or '')
    except ValueError:
        return False
    return True


def handler_for(dashboard, allowed_hosts=()):
    allowed_hosts = {name.lower() for name in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                authority = self.headers.get('Host', '').lower()
                host = urlsplit('http://' + authority)
                port = self.server.server_address[1]
                if not trusted_hostname(host.hostname, allowed_hosts) or host.port != port or host.path:
                    return self.respond(403, {'error': 'Invalid Host'})
                origin = self.headers.get('Origin')
                if origin and origin.lower() != f'http://{authority}':
                    return self.respond(403, {'error': 'Invalid Origin'})
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    return self.respond(403, {'error': 'Cross-site request forbidden'})
                url = urlsplit(self.path)
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True) if url.query else {}
                if any(len(v) != 1 for v in query.values()):
                    raise ValueError('Repeated query argument')
                args = {k: v[0] for k, v in query.items()}
                routes = {'/api/overview': dashboard.overview, '/api/cases': dashboard.cases,
                          '/api/case': dashboard.detail, '/api/source': dashboard.source}
                if url.path == '/api/catalog' and not args:
                    return self.respond(200, dashboard.catalog())
                if url.path in routes:
                    if 'id' in args:
                        args['case_id'] = args.pop('id')
                    args.setdefault('dataset', 'demo')
                    return self.respond(200, routes[url.path](**args))
                static = {'/': 'dashboard.html', '/dashboard': 'dashboard.html', '/dashboard.html': 'dashboard.html',
                          '/dashboard.js': 'dashboard.js', '/dashboard.css': 'dashboard.css',
                          '/appeal_controls.js': 'appeal_controls.js'}
                if url.path in static and not args:
                    file = ROOT / static[url.path]
                    return self.respond(200, file.read_bytes(), {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}[file.suffix[1:]])
                self.respond(404, {'error': 'Unknown endpoint'})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.respond(400, {'error': str(exc) if not isinstance(exc, TypeError) else 'Invalid query arguments'})
            except (KeyError, FileNotFoundError):
                self.respond(404, {'error': 'Artifact or case not found'})
            except OSError:
                self.respond(400, {'error': 'Artifact could not be read'})

        def respond(self, status, value, content_type='application/json'):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=DEFAULT_HOST, help='Bind address (default: all interfaces)')
    parser.add_argument('--port', type=int, default=8771)
    parser.add_argument('--allowed-host', action='append', default=[],
                        help='Additional hostname accepted in Host headers; IP addresses need no entry')
    parser.add_argument('--baseline', type=Path, help='A run directory')
    parser.add_argument('--candidate', type=Path, help='A run directory')
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--experiments', type=Path, help='Experiment directory to list (default: data/experiments)')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        dashboard = Dashboard(baseline=args.baseline, candidate=args.candidate, reference=args.reference,
                              experiments=args.experiments)
    except ValueError as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer((args.host, args.port), handler_for(dashboard, args.allowed_host))
    print(f'Appeal dashboard: {display_url(args.host, args.port, "/dashboard")}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
