"""The web app's JSON API. Every function reads; none writes.

    /api/home                     running runs, recent runs, production, experiments, named datasets
    /api/runs                     every run, with its live state and spending
    /api/run?name=                one run: record, live progress by step, spending by step and over time
    /api/run/items?name=          its items: where each is, or how it ended
    /api/run/trace?name=&key=     one item's trace (saved when the run finished)
    /api/datasets                 every dataset, with names and producers
    /api/dataset?id=              manifest, the kind's report, and a page of items
    /api/item?id=&key=            one item, shaped for its kind's viewer
    /api/page-image?doc=&page=    a PDF page rendered as PNG
    /api/pipelines                resolved pipelines with tags and runs
    /api/pipeline?id=[&other=]    a step tree, or two trees and their diff
    /api/lab/...                  the lab's views (court.lab.dashboard)
"""
from collections import Counter
from datetime import datetime, timezone
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from court.contracts import cases as case_contract, pages as page_contract, results as contract
from court.core.datasets import Datasets, is_failure
from court.core.pipelines import Pipelines
from court.core.raw import Raw
from court.core.runner import Runner
from court.lab import compare
from court.lab.dashboard import Dashboard
from court.reports import cases as case_report, text as text_report

SHA = re.compile(r'[0-9a-f]{64}\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]*\Z')


def page_of(rows, offset, limit):
    offset, limit = int(offset), int(limit)
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError('Invalid pagination')
    return {'rows': rows[offset:offset+limit], 'total': len(rows), 'offset': offset, 'limit': limit}


class Api:
    def __init__(self, dashboard=None, raw=None):
        self.lab = dashboard or Dashboard()
        self.pipelines, self.datasets, self.runner = self.lab.pipelines, self.lab.datasets, self.lab.runner
        self.raw = raw or Raw()
        self.loaded, self.lock = {}, threading.Lock()

    # Shared ------------------------------------------------------------------
    def dataset(self, ref):
        """A dataset's items, loaded once (datasets never change)."""
        found = self.datasets.open(ref)
        with self.lock:
            if found.id not in self.loaded:
                if len(self.loaded) > 6:
                    self.loaded.pop(next(iter(self.loaded)))
                self.loaded[found.id] = (found, found.load())
            return self.loaded[found.id]

    def names(self):
        found = {}
        for name, identity in self.datasets.names().items():
            found.setdefault(identity, []).append(name)
        return found

    def tags(self):
        found = {}
        for tag, entry in self.pipelines.tags().items():
            found.setdefault(entry['pipeline'], []).append(tag)
        return found

    def run_row(self, record):
        live = self.runner.live(record['name'])
        record, progress, spending = live['record'], live['progress'], live['spending']
        total = (progress or {}).get('total') or (record.get('items') or {}).get('total')
        done = (record.get('items') or {}).get('total') if live['state'] != 'running' else None
        return {'name': record['name'], 'pipeline': record['pipeline'], 'at': record['at'] or '(root)',
                'state': live['state'], 'labels': record.get('labels') or {}, 'note': record.get('note'),
                'mode': record['options']['mode'], 'limit_usd': record['options']['limit_usd'],
                'keys': len(record['keys']) if record.get('keys') else None, 'total': total, 'done': done,
                'failed': (record.get('items') or {}).get('failed') or {},
                'spent_usd': spending['spent_usd'], 'charged_usd': spending['charged_usd'],
                'outstanding_usd': spending['outstanding_usd'], 'would_spend_usd': spending['would_spend_usd'],
                'reused_value_usd': spending['reused_value_usd'], 'calls': spending['calls'],
                'started_at': record.get('execution_started_at') or record.get('started_at'),
                'finished_at': record.get('finished_at'), 'executions': record.get('executions'),
                'history': [execution_row(e) for e in live['executions']],
                'working_seconds': sum(execution_row(e)['seconds'] or 0 for e in live['executions']),
                'unrecorded': max(0, (record.get('executions') or 1) - len(live['executions'])),
                'current': [s['path'] for s in (progress or {}).get('steps', []) if not s.get('finished_at')]
                if live['state'] == 'running' else []}

    # Home and runs -------------------------------------------------------------
    def home(self):
        runs = self.runs()['runs']
        catalog = self.lab.catalog()
        production = None
        try:
            resolved = self.pipelines.load('production')
            view = self.lab.view('production')
            metrics = compare.profile(view['sides']['baseline'].values())['metrics'] if view.get('sides') else None
            production = {'pipeline': resolved['id'], 'name': resolved.get('name'),
                          'description': resolved.get('description'), 'metrics': metrics, 'output': view.get('output')}
        except (KeyError, ValueError):
            pass
        return {'running': [r for r in runs if r['state'] == 'running'], 'recent': runs[:8],
                'production': production, 'experiments': [d for d in catalog['datasets'] if d['kind'] == 'experiment'],
                'datasets': [{'id': i, 'names': n} for i, n in self.names().items()], 'tags': catalog['tags']}

    def runs(self):
        rows = [self.run_row(r) for r in self.runner.all()]
        rows.sort(key=lambda r: (r['state'] != 'running', r['started_at'] or ''), reverse=False)
        running = [r for r in rows if r['state'] == 'running']
        rest = sorted([r for r in rows if r['state'] != 'running'], key=lambda r: r['started_at'] or '', reverse=True)
        return {'runs': running + rest, 'tags': self.tags()}

    def run(self, name, execution=''):
        """One run, as one of its executions (the latest by default), with the run's totals."""
        live = self.runner.live(name)
        record = live['record']
        chosen, latest = pick_execution(live['executions'], execution)
        progress = (live['progress'] or {}) if latest else {'steps': chosen.get('steps') or []}
        spending = chosen['spending']
        steps = []
        for step in progress.get('steps', []):
            spent = next((s for s in spending['steps'] if s['step'] == step['path']), None)
            entry = {**step, 'spending': spent}
            if latest and not step.get('finished_at') and live['state'] == 'running' and step.get('inputs'):
                entry['eta_seconds'] = eta(step)
            steps.append(entry)
        known = {s['path'] for s in steps}
        resolved = self.pipelines.load(record['pipeline'])
        tree = self.lab.descriptor(resolved)
        base = (resolved.get('definition') or {}).get('base')
        return {'pipeline': {'name': resolved.get('name'), 'base': base,
                             'base_name': self.pipelines.load(base).get('name') if base else None,
                             'base_tags': self.tags().get(base, []) if base else []},
                'record': record, 'state': live['state'] if latest else chosen['status'], 'running': live['running'],
                'updated_at': progress.get('updated_at'), 'total': progress.get('total'), 'steps': steps,
                'execution': execution_row(chosen), 'latest': latest,
                'executions': [execution_row(e) for e in live['executions']],
                'unrecorded': max(0, (record.get('executions') or 1) - len(live['executions'])),
                'spending': {k: v for k, v in spending.items() if k != 'steps'},
                'run_spending': {k: v for k, v in live['spending'].items() if k not in ('steps', 'timeline')},
                'unlabeled_steps': [s for s in spending['steps'] if s['step'] not in known],
                'items': dict(Counter(item_state(i) for i in progress.get('items', {}).values())),
                'tree': tree, 'tags': self.tags().get(record['pipeline'], []),
                'datasets': {path: {'id': i, 'kind': self.datasets.open(i).kind}
                             for path, i in (record.get('outputs') or {}).items()} if latest else {}}

    def run_items(self, name, q='', state='', offset='0', limit='100', execution=''):
        live = self.runner.live(name)
        record, progress = live['record'], live['progress'] or {}
        chosen, latest = pick_execution(live['executions'], execution)
        if not latest:
            # A past execution: its items are its final output, plus what its trace noted outside it.
            record, progress = {'output': chosen.get('output')}, {'items': {}}
            if chosen.get('traces'):
                traces = self.dataset(chosen['traces'])[1]
                progress['items'] = {k: {'step': e[-1]['step'], 'event': e[-1]['event'], 'kind': e[-1].get('kind')}
                                     for k, e in traces.items() if e}
        items = {k: {**v, 'state': item_state(v)} for k, v in progress.get('items', {}).items()}
        # A step noting something under its own name, not an item's, such as a download it skipped.
        for key, item in items.items():
            if key == item.get('step') and not item.get('failed'):
                item['state'] = 'note'
        if (live['state'] != 'running' or not latest) and record.get('output'):
            output = self.dataset(record['output'])[1]
            for key, value in output.items():
                base = items.get(key, {})
                items[key] = {**base, 'state': 'failed' if is_failure(value) else 'done',
                              'kind': value.kind if is_failure(value) else base.get('kind'),
                              'reason': value.reason if is_failure(value) else None,
                              'step': value.step if is_failure(value) else base.get('step')}
        rows = [{'key': k, **v} for k, v in sorted(items.items())
                if q.casefold() in k.casefold() and (not state or v['state'] == state)]
        return {**page_of(rows, offset, limit), 'output': record.get('output'),
                'states': dict(Counter(v['state'] for v in items.values()))}

    def run_trace(self, name, key, execution=''):
        live = self.runner.live(name)
        record, (chosen, latest) = live['record'], pick_execution(live['executions'], execution)
        if not latest:
            record = {'traces': chosen.get('traces')}
        if record.get('traces'):
            traces = self.datasets.open(record['traces'])
            return {'key': key, 'events': traces.get(key) if key in traces else [], 'final': True}
        progress = live['progress'] or {}
        return {'key': key, 'events': [progress.get('items', {}).get(key)] if key in progress.get('items', {}) else [],
                'final': False}

    # Datasets ---------------------------------------------------------------
    def datasets_list(self):
        names = self.names()
        rows = []
        for d in self.datasets.all():
            producer = d.manifest.get('producer') or {}
            rows.append({'id': d.id, 'kind': d.kind, 'items': len(d), 'names': names.get(d.id, []),
                         'contract': d.manifest.get('contract'), 'created_at': d.manifest.get('created_at'),
                         'run': producer.get('run'), 'position': producer.get('position'),
                         'pipeline': producer.get('pipeline')})
        rows.sort(key=lambda r: (not r['names'], r['kind'] == 'traces', r['kind'] == 'output', r['created_at'] or ''))
        return {'datasets': rows}

    def dataset_view(self, id, q='', offset='0', limit='50'):
        found, values = self.dataset(id)
        manifest = {k: v for k, v in found.manifest.items() if k != 'items'}
        report = None
        if found.kind == 'cases':
            full = case_report.report(values)
            report = {**{k: v for k, v in full.items() if k not in ('flags', 'weak_documents', 'membership_doubts')},
                      'flags': {k: v['count'] for k, v in full['flags'].items()},
                      'weak_documents': len(full['weak_documents'])}
        elif found.kind == 'pages':
            full = text_report.audit(values)
            report = {**{k: v for k, v in full.items() if k != 'documents_with_unresolved_pages'},
                      'documents_with_unresolved_pages': len(full['documents_with_unresolved_pages'])}
        elif found.kind == 'results':
            report = compare.profile(values.values())
        elif found.kind == 'documents':
            listings = [e for v in values.values() for e in v['listings']]
            report = {'documents': len(values), 'listing_entries': len(listings),
                      'sources': dict(Counter(v['source'] for v in values.values())),
                      'bytes': sum(v['bytes'] for v in values.values())}
        rows = [summary_row(found.kind, k, v) for k, v in values.items()
                if not q or q.casefold() in (k + ' ' + search_text(found.kind, v)).casefold()]
        return {'id': found.id, 'kind': found.kind, 'names': self.names().get(found.id, []), 'manifest': manifest,
                'report': report, **page_of(rows, offset, limit)}

    def item(self, id, key, pages=''):
        found, values = self.dataset(id)
        if key not in values:
            raise KeyError(key)
        value = values[key]
        shaped = {'id': found.id, 'kind': found.kind, 'key': key}
        if is_failure(value):
            return {**shaped, 'failure': value.to_json()}
        if found.kind == 'pages':
            siblings = sorted(v['page'] for k, v in values.items() if k.startswith(value['doc']+'/') and not is_failure(v))
            return {**shaped, 'value': value, 'page_count': len(siblings)}
        if found.kind == 'cases':
            text = value['text']
            by_page = case_contract.pages(text)
            return {**shaped, 'value': {k: v for k, v in value.items() if k != 'text'},
                    'pages': {str(d): {str(p): t for p, t in ps.items()} for d, ps in by_page.items()},
                    'text_chars': len(text)}
        if found.kind == 'results':
            cases = self.case_set_for(found)
            facts = compare.compare_case(key, value, None)['one_sided']
            unverified = []
            if cases is not None and key in cases:
                unverified = contract.verify_quotes(value, case_contract.pages(cases[key]['text']))[1]
            return {**shaped, 'value': value, 'facts': facts, 'unverified': unverified,
                    'cases': self.case_set_id(found)}
        if found.kind == 'documents':
            return {**shaped, 'value': value, 'pages': pages_for(self, value['sha256'], pages)}
        return {**shaped, 'value': value}

    def case_set_id(self, results):
        producer = results.manifest.get('producer') or {}
        for identity in (producer.get('inputs') or {}).values():
            if self.datasets.open(identity).kind == 'cases':
                return identity
        if producer.get('run'):
            try:
                record = self.runner.load(producer['run'])
            except Exception:
                return None
            for identity in [*(record.get('outputs') or {}).values(), *(record.get('inputs') or {}).values()]:
                if self.datasets.open(identity).kind == 'cases':
                    return identity
        names = self.datasets.names()
        return names.get('legacy-cases') if results.id == names.get('legacy-results') else None

    def case_set_for(self, results):
        identity = self.case_set_id(results)
        return self.dataset(identity)[1] if identity else None

    def source(self, id, key):
        """The case text a results item cites, verified against its hash."""
        found = self.datasets.open(id)
        cases = self.case_set_for(found) if found.kind == 'results' else self.dataset(id)[1]
        if cases is None or key not in cases:
            raise KeyError(key)
        case = cases[key]
        return {'case_id': key, 'text': case['text'], 'sha256': case['text_sha256'], 'verified': True}

    def page_image(self, doc, page):
        if not SHA.fullmatch(doc) or not page.isdecimal():
            raise ValueError('Expected a PDF hash and a page number')
        with tempfile.TemporaryDirectory(prefix='court-ui-') as folder:
            output = Path(folder)/'page'
            subprocess.run(['pdftoppm', '-f', page, '-l', page, '-scale-to', '1600', '-png', '-singlefile',
                            str(self.raw.pdf(doc)), str(output)], check=True, capture_output=True, timeout=90)
            return output.with_suffix('.png').read_bytes()

    def page_text(self, id, doc, page):
        found, values = self.dataset(id)
        value = values.get(page_contract.key(doc, int(page)))
        if value is None or is_failure(value):
            raise KeyError(f'{doc}/{page}')
        return value

    # Pipelines ------------------------------------------------------------
    def pipelines_list(self):
        tags = self.tags()
        runs = Counter(r['pipeline'] for r in self.runner.all())
        rows = [{'id': p['id'], 'name': p.get('name'), 'description': p.get('description'), 'tags': tags.get(p['id'], []),
                 'created_at': p.get('created_at'), 'runs': runs.get(p['id'], 0),
                 'base': (p.get('definition') or {}).get('base')} for p in self.pipelines.all()]
        rows.sort(key=lambda r: r['created_at'] or '', reverse=True)
        return {'pipelines': rows, 'log': self.pipelines.tag_log()}

    def pipeline(self, id, other=''):
        resolved = self.pipelines.load(id)
        trees = self.lab.trees(other, resolved['id']) if other else self.lab.trees(resolved['id'], None)
        runs = [self.run_row(r) for r in self.runner.all() if r['pipeline'] == resolved['id']]
        return {'id': resolved['id'], 'name': resolved.get('name'), 'description': resolved.get('description'),
                'definition': resolved.get('definition'), 'created_at': resolved.get('created_at'),
                'commit': resolved.get('commit'), 'tags': self.tags().get(resolved['id'], []),
                'pipelines': trees, 'runs': runs,
                'log': [e for e in self.pipelines.tag_log() if e['pipeline'] == resolved['id']]}


def eta(step):
    """Seconds left for a running step, from how many of its items it has touched so far."""
    touched, inputs = step.get('touched') or 0, step.get('inputs') or 0
    if touched < 3 or touched >= inputs:
        return None
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(step['started_at'])).total_seconds()
    return round(elapsed * (inputs - touched) / touched)


def pick_execution(executions, number=''):
    """The execution numbered ``number``, or the latest; and whether it is the latest."""
    chosen = next((e for e in executions if str(e['number']) == str(number)), executions[-1])
    return chosen, chosen is executions[-1]


def execution_row(entry):
    """An execution's summary: when, how long, how it was started and ended, and what it sent and charged."""
    spending = entry.get('spending') or {}
    start, end = entry.get('started_at'), entry.get('finished_at')
    seconds = None
    if start and (end or entry.get('status') == 'running'):
        finish = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
        seconds = round((finish - datetime.fromisoformat(start)).total_seconds(), 1)
    return {'number': entry['number'], 'started_at': start, 'finished_at': end, 'seconds': seconds,
            'status': entry.get('status'), 'options': entry.get('options') or {}, 'items': entry.get('items'),
            'error': entry.get('error'), 'charged_usd': spending.get('charged_usd', 0),
            'outstanding_usd': spending.get('outstanding_usd', 0), 'calls': spending.get('calls') or {},
            'tokens': spending.get('tokens') or {}}


def item_state(item):
    if item.get('state'):
        return item['state']
    return 'failing' if item.get('failed') else 'working'


def search_text(kind, value):
    if is_failure(value):
        return value.reason
    if kind == 'cases':
        return ' '.join(value['dockets'])
    if kind == 'results':
        return ' '.join(r['docket'] for r in value['records'])
    if kind == 'documents':
        return ' '.join(e['title'] or '' for e in value['listings'])
    return ''


def summary_row(kind, key, value):
    if is_failure(value):
        return {'key': key, 'failure': value.to_json()}
    if kind == 'cases':
        return {'key': key, 'dockets': value['dockets'], 'documents': len(value['documents']),
                'composition': value['composition'], 'chars': len(value['text']),
                'flags': sum(len(d['flags']) for d in value['documents']),
                'weak': sum(d['basis'] in ('listing_only', 'none', 'disagree') for d in value['documents'])}
    if kind == 'pages':
        return {'key': key, 'doc': value['doc'], 'page': value['page'], 'status': value['status'],
                'source': value['source'], 'reason': value['reason'], 'chars': len(value['text'] or '')}
    if kind == 'results':
        return {'key': key, 'disposition': (value['disposition'] or {}).get('value'), 'records': len(value['records']),
                'dockets': [r['docket'] for r in value['records']]}
    if kind == 'documents':
        first = value['listings'][0] if value['listings'] else {}
        return {'key': key, 'title': first.get('title'), 'posted': first.get('date_attribute'),
                'docket': first.get('docket_raw'), 'listings': len(value['listings']), 'bytes': value['bytes']}
    text = repr(value)
    return {'key': key, 'preview': text[:160]}


def pages_for(api, sha, pages_ref):
    """A document's page numbers, from a pages dataset (by default the newest named one)."""
    ref = pages_ref or next((i for n, i in sorted(api.datasets.names().items()) if n.endswith('pages')), None)
    if not ref:
        return {'dataset': None, 'pages': []}
    found, values = api.dataset(ref)
    numbers = sorted(v['page'] for k, v in values.items() if k.startswith(sha+'/') and not is_failure(v))
    return {'dataset': found.id, 'pages': numbers}
