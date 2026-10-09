"""Read-only, loopback dashboard for frozen appeal experiment artifacts."""
import argparse
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from appeal_demo import build_demo
from appeal_evaluation import evaluate
from appeal_experiments import read_experiment
from appeal_reference import read_reference

ROOT = Path(__file__).resolve().parent
CLASSIFICATIONS = {'corrected', 'regressed', 'unchanged_error', 'changed_unscored',
                   'unchanged_unscored', 'unchanged_correct', 'unscored', 'missing',
                   'extra', 'failures', 'incompatible', 'ambiguous'}


def signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class Dashboard:
    def __init__(self, production=None, baseline=None, candidate=None, reference=None):
        self.production = Path(production or ROOT / 'data/experiments/production-baseline')
        self.configured = [Path(p) if p else None for p in (baseline, candidate, reference)]
        if candidate and not baseline or reference and not candidate:
            raise ValueError('A candidate requires a baseline; a reference requires both runs')
        self.cache = {}
        self.lock = threading.RLock()

    def catalog(self):
        datasets = [{'id': 'demo', 'label': 'Synthetic software test', 'available': True, 'kind': 'synthetic'},
                    {'id': 'production', 'label': 'Historical production baseline',
                     'available': (self.production / 'manifest.json').is_file(), 'kind': 'historical_baseline'}]
        if self.configured[0]:
            datasets.append({'id': 'configured', 'label': 'Supplied experiment artifacts', 'available': True, 'kind': 'comparison'})
        return {'datasets': datasets, 'default': 'demo'}

    def load(self, dataset):
        with self.lock:
            if dataset == 'demo':
                if dataset not in self.cache:
                    self.cache[dataset] = (None, build_demo())
                return self.cache[dataset][1]
            if dataset == 'production':
                paths = [self.production, None, None]
            elif dataset == 'configured' and self.configured[0]:
                paths = self.configured
            else:
                raise ValueError('Unknown dataset')
            files = [p / name for p in paths[:2] if p for name in ('manifest.json', 'cases.json')]
            if paths[2]:
                files.append(paths[2])
            before = tuple(signature(p) for p in files)
            if dataset in self.cache and self.cache[dataset][0] == before:
                return self.cache[dataset][1]
            runs = [read_experiment(p) if p else None for p in paths[:2]]
            reference = read_reference(paths[2]) if paths[2] else None
            value = {'baseline': runs[0]['canonical'], 'candidate': runs[1]['canonical'] if runs[1] else None,
                     'reference': reference, 'evaluation': evaluate(*runs, reference) if runs[1] else None,
                     'notice': 'Frozen saved artifacts; no extraction or reference generation is performed.',
                     'roots': {side: Path(run['manifest']['source_run']).resolve()
                               for side, run in zip(('baseline', 'candidate'), runs) if run},
                     'manifests': {side: run['manifest'] for side, run in zip(('baseline', 'candidate'), runs) if run}}
            if before != tuple(signature(p) for p in files):
                raise ValueError('Artifacts changed during read')
            self.cache[dataset] = (before, value)
            return value

    def selection(self, value):
        return {'rationale': 'Deterministic invented software-test cases; not a research sample.' if value.get('purpose') == 'synthetic_software_test'
                else 'Cases included in the supplied frozen artifact; no reference cohort has been selected here.'}

    def overview(self, dataset):
        value = self.load(dataset)
        runs, designs = [], {}
        for side in ('baseline', 'candidate'):
            run = value.get(side)
            designs[side] = {'recipe': run.get('recipe', {}), 'prompts': run.get('prompts', {})} if run else None
            if run:
                runs.append({'side': side, 'name': run.get('run', {}).get('name', side),
                             'status_counts': dict(Counter(c.get('execution', {}).get('status', 'unknown') for c in run['cases'])),
                             'case_count': len(run['cases']), 'record_count': sum(len(c.get('records', [])) for c in run['cases']),
                             'cost': run.get('provenance', {}).get('cost'),
                             'execution': value.get('manifests', {}).get(side, {}).get('execution', value.get('execution'))})
        evaluation = value.get('evaluation')
        return {'dataset': dataset, 'notice': value['notice'], 'synthetic': dataset == 'demo',
                'summary': evaluation['summary'] if evaluation else None, 'runs': runs, 'designs': designs,
                'reference': {k: v for k, v in value['reference'].items() if k != 'cases'} if value.get('reference') else None,
                'selection': self.selection(value), 'design_diff': self.design_diff(designs)}

    @staticmethod
    def design_diff(designs):
        b, c = designs['baseline'], designs['candidate']
        if not c:
            return None
        components = [{x['id']: x for x in d['recipe'].get('components', [])} for d in (b, c)]
        return {'recipe_changed': b['recipe'] != c['recipe'], 'prompts_changed': b['prompts'] != c['prompts'],
                'changed_components': sorted(k for k in set(components[0]) | set(components[1]) if components[0].get(k) != components[1].get(k))}

    def detail(self, dataset, case_id):
        value = self.load(dataset)
        result = {'case_id': case_id, 'selection': self.selection(value)}
        for key in ('baseline', 'candidate', 'reference', 'evaluation'):
            result[key] = next((c for c in (value.get(key) or {}).get('cases', []) if c['case_id'] == case_id), None)
        if not any(result[k] for k in ('baseline', 'candidate', 'reference')):
            raise KeyError('Unknown case')
        return result

    def cases(self, dataset, q='', status='', scope='', classification='', offset='0', limit='50'):
        offset, limit = int(offset), int(limit)
        if offset < 0 or not 1 <= limit <= 200 or len(q) > 200:
            raise ValueError('Invalid pagination or search')
        if classification and classification not in CLASSIFICATIONS:
            raise ValueError('Unknown classification')
        value = self.load(dataset)
        indexed = {key: {c['case_id']: c for c in (value.get(key) or {}).get('cases', [])}
                   for key in ('baseline', 'candidate', 'reference', 'evaluation')}
        rows = []
        for case_id in sorted(set().union(*(set(v) for v in indexed.values()))):
            b, c, ev = (indexed[key].get(case_id) for key in ('baseline', 'candidate', 'evaluation'))
            chosen = b or c or {}
            statuses = {s: r.get('execution', {}).get('status', 'unknown') if r else None for s, r in (('baseline', b), ('candidate', c))}
            counts = dict(Counter(f['classification'] for f in (ev or {}).get('fields', [])))
            flags = []
            if ev:
                if not ev['compatibility']['compatible']:
                    flags.append('incompatible')
                for flag, key in (('missing', 'missing'), ('extra', 'additional'), ('ambiguous', 'ambiguous')):
                    if any(ev['records'][s][key] or (key == 'ambiguous' and ev['records'][s].get('null_singleton_matches')) for s in ('baseline', 'candidate')):
                        flags.append(flag)
                if any(counts.get(k) for k in ('changed_unscored', 'unchanged_unscored')) or not ev['summary']['comparable_fields']:
                    flags.append('unscored')
            else:
                flags.append('unscored')
            if any(s and (s in ('failed', 'error', 'interrupted', 'incomplete') or s.endswith('_failed')) for s in statuses.values()):
                flags.append('failures')
            dockets = sorted({str(d) for r in (b, c) if r for d in r.get('source', {}).get('dockets', []) + [x.get('docket') for x in r.get('records', [])] if d})
            row = {'case_id': case_id, 'dockets': dockets, 'scope_category': chosen.get('scope_category', 'unknown'),
                   'status': statuses, 'record_count': {s: len(r.get('records', [])) if r else None for s, r in (('baseline', b), ('candidate', c))},
                   'classifications': counts, 'flags': flags, 'source_sha256': chosen.get('source', {}).get('sha256')}
            if q.casefold() not in (' '.join([case_id] + dockets)).casefold():
                continue
            running = {'running', 'reviewing', 'extracting', 'triaging', 'measuring'}
            status_matches = (any(s in running for s in statuses.values()) if status == 'running'
                              else any(s and (s in ('failed', 'error') or s.endswith('_failed')) for s in statuses.values()) if status == 'failed'
                              else 'complete' in statuses.values() if status == 'completed'
                              else status in statuses.values())
            if status and not status_matches or scope and scope != row['scope_category']:
                continue
            if classification and not (counts.get(classification) or classification in flags):
                continue
            rows.append(row)
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
            root = value['roots'][side]
            relative = Path(case['source']['path'])
            if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'sources':
                raise ValueError('Unsafe source path')
            path = root / relative
            current = root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError('Source symlinks are forbidden')
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root / 'sources'):
                raise ValueError('Source outside approved root')
            before = signature(resolved)
            raw = resolved.read_bytes()
            if before != signature(resolved):
                raise ValueError('Source changed during read')
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected.lower():
            raise ValueError('Source hash mismatch; text withheld')
        return {'case_id': case_id, 'side': side, 'text': raw.decode('utf-8'), 'sha256': digest, 'verified': True}


def handler_for(dashboard):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                host = urlsplit('http://' + self.headers.get('Host', ''))
                port = self.server.server_address[1]
                if host.hostname not in ('127.0.0.1', 'localhost') or host.port != port or host.path:
                    return self.respond(403, {'error': 'Invalid Host'})
                origin = self.headers.get('Origin')
                if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
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
    parser.add_argument('--port', type=int, default=8771)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        dashboard = Dashboard(baseline=args.baseline, candidate=args.candidate, reference=args.reference)
    except ValueError as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(dashboard))
    print(f'Appeal dashboard: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
