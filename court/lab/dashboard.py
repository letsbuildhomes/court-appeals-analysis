"""The lab's views for the web app: experiments, their rounds, and production's quality profile.

Views (the ``dataset`` argument of every API call):

    production                the pipeline the production tag names, with its latest full run's results
    experiment:NAME:ROUND     one experiment round: baseline versus candidate results

It reads experiment records, runs, datasets and resolved pipelines, and
changes nothing.
"""
from collections import Counter
from pathlib import Path
import threading

from court.contracts import cases as case_contract, results as contract
from court.core.datasets import Datasets, is_failure
from court.core.hashing import sha256
from court.core.pipelines import Pipelines, diff as pipeline_diff, walk
from court.core.runner import Runner
from court.lab import compare
from court.lab.experiments import Lab

VERDICTS = ('worsened', 'mixed', 'changed', 'improved', 'unchanged')
PRODUCTION = ('The pipeline the production tag names, with the results of its latest run over every case. Its '
              'quality profile shows where answers are missing: where experiments can look for improvements.')


def selected(record):
    """An experiment's listed probe keys; none when its probe chooses them when it runs."""
    probe = record.get('probe', [])
    return [] if isinstance(probe, str) else probe


class Dashboard:
    def __init__(self, lab=None):
        self.lab = lab or Lab()
        self.pipelines, self.datasets, self.runner = self.lab.pipelines, self.lab.datasets, self.lab.runner
        self.cache, self.summaries, self.lock = {}, {}, threading.RLock()

    # Loading -----------------------------------------------------------------
    def view(self, dataset):
        with self.lock:
            key = (dataset, self.signature())
            if key not in self.cache:
                self.cache = {key: self.load(dataset)}
            return self.cache[key]

    def signature(self):
        files = [*self.lab.root.glob('*/experiment.json'), *self.lab.root.glob('*/rounds/*.json'), *self.lab.root.glob('*/plans/*.json'),
                 *self.runner.root.glob('*/run.json'), self.pipelines.root/'tags.jsonl']
        return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in files if p.exists()))

    def load(self, dataset):
        if dataset == 'production':
            return self.production()
        kind, _, rest = dataset.partition(':')
        name, _, round_name = rest.partition(':')
        if kind != 'experiment' or not name:
            raise ValueError('Unknown view')
        record = self.lab.load(name)
        rounds = self.lab.rounds(name)
        if round_name not in rounds:
            return {'kind': 'experiment', 'status': 'planned', 'experiment': record, 'round': round_name or 'probe',
                    'plan': self.lab.plans(name).get(round_name or 'probe'),
                    'pipelines': self.trees(record['baseline'], self.lab.candidate(record)['id'])}
        result = rounds[round_name]
        baseline = self.datasets.open(result['baseline']['output']).load(result['keys'])
        candidate = self.datasets.open(result['candidate']['output']).load(result['keys'])
        reference = self.datasets.open(record['reference']).load(result['keys']) if record.get('reference') else None
        settled = result['baseline'].get('settled', True)
        comparison = compare.compare(baseline, candidate, reference, result['keys'], settled=settled,
                                     inputs=result.get('inputs'))
        runs = {'baseline': self.runner.load(result['baseline']['run']),
                'candidate': self.runner.load(result['candidate']['run'])}
        return {'kind': 'experiment', 'status': 'finalized', 'experiment': record, 'round': round_name,
                'result': result, 'sides': {'baseline': baseline, 'candidate': candidate}, 'reference': reference,
                'settled': settled,
                'comparison': comparison, 'runs': runs, 'cases': self.case_set(runs['candidate']),
                'pipelines': self.trees(result['baseline']['pipeline'], result['candidate']['pipeline'])}

    def production(self):
        resolved = self.pipelines.load('production')
        full = [r for r in self.runner.all() if r['pipeline'] == resolved['id'] and r['keys'] is None
                and any(self.datasets.open(d).kind == 'results' for d in r.get('outputs', {}).values())]
        if not full:
            return {'kind': 'baseline', 'status': 'empty', 'pipelines': self.trees(resolved['id'], None)}
        run = max(full, key=lambda r: r['finished_at'])
        output = next(d for d in run['outputs'].values() if self.datasets.open(d).kind == 'results')
        values = self.datasets.open(output).load()
        return {'kind': 'baseline', 'status': 'finalized', 'sides': {'baseline': values}, 'runs': {'baseline': run},
                'settled': True,
                'cases': self.case_set(run), 'pipelines': self.trees(resolved['id'], None), 'output': output}

    def case_set(self, run):
        """The case set a run's results came from."""
        for identity in [*run.get('outputs', {}).values(), *run.get('inputs', {}).values()]:
            dataset = self.datasets.open(identity)
            if dataset.kind == 'cases':
                return dataset
        return None

    def trees(self, baseline, candidate):
        designs = {'baseline': self.pipelines.load(baseline), 'candidate': self.pipelines.load(candidate) if candidate else None}
        rows = pipeline_diff(designs['baseline'], designs['candidate']) if candidate else []
        return {'baseline': self.descriptor(designs['baseline']),
                'candidate': self.descriptor(designs['candidate']) if candidate else None,
                'ids': {side: (d or {}).get('id') for side, d in designs.items()},
                'diff': [self.diff_row(r, designs) for r in rows]}

    def descriptor(self, resolved):
        """A resolved tree in the shape the tree view draws."""
        library = self.pipelines.library
        texts = {}

        def prompt(name):
            if name not in texts:
                texts[name] = library.prompt(name, resolved['prompts'][name])[0]
            return texts[name]

        def node(n, path):
            cls = library.step_class(n['step'], resolved['library'][n['step']]['hash'])
            settings = {**cls.defaults, **n.get('settings', {})}
            model = settings.get('model')
            return {'name': n['name'], 'path': path, 'type': n['step'], 'description': cls.description,
                    'model': {'model': model, 'settings': {k: v for k, v in settings.items() if k != 'model'}}
                    if model else None,
                    'conversation': ('continue' if settings.get('continue') else 'fresh') if model else None,
                    'params': {} if model else settings, 'save': n.get('save'),
                    'prompts': {slot: {'file': name, 'text': prompt(name)} for slot, name in n.get('prompts', {}).items()},
                    'code': {n['step']: resolved['library'][n['step']]['hash']},
                    'children': [node(c, f'{path}/{c["name"]}' if path else c['name']) for c in n.get('children', [])]}
        # The top of the tree is the pipeline itself, named as the pipeline rather than as the step it inherited.
        return {**node(resolved['root'], ''), 'name': resolved.get('name') or resolved['root']['name']}

    def diff_row(self, row, designs):
        result = {'path': row['path'], 'status': row['change']}
        if row['change'] != 'changed':
            return result
        fields = []
        if 'step' in row:
            fields.append({'field': 'step', 'base': row['step'][0], 'value': row['step'][1]})
        before, after = row.get('settings', [None, None])
        for key in sorted(set(before or {}) | set(after or {})):
            if (before or {}).get(key) != (after or {}).get(key):
                fields.append({'field': key, 'base': (before or {}).get(key), 'value': (after or {}).get(key)})
        if 'save' in row:
            fields.append({'field': 'save', 'base': row['save'][0], 'value': row['save'][1]})
        result['fields'] = fields
        prompts = []
        if 'prompts' in row:
            old, new = row['prompts'][0] or {}, row['prompts'][1] or {}
            library = self.pipelines.library
            for slot in sorted(set(old) | set(new)):
                if old.get(slot) != new.get(slot):
                    prompts.append({'name': slot, 'status': f'{old.get(slot)} → {new.get(slot)}',
                                    'base': library.prompt(old[slot])[0] if slot in old else '',
                                    'value': library.prompt(new[slot])[0] if slot in new else ''})
        result['prompts'] = prompts
        if 'children' in row:
            result['reordered'] = True
        return result

    # API ---------------------------------------------------------------------
    def catalog(self):
        cards = []
        for record in sorted(self.lab.all(), key=lambda r: r['created_at'], reverse=True):
            rounds = self.lab.rounds(record['name'])
            for round_name in [r for r in ('confirm', 'probe') if r in rounds] or ['probe']:
                dataset = f'experiment:{record["name"]}:{round_name}'
                try:
                    cards.append(self.card(dataset))
                except (ValueError, KeyError, OSError) as exc:
                    cards.append({'id': dataset, 'label': record['name'], 'kind': 'experiment', 'available': False,
                                  'error': str(exc)})
        cards.append({'id': 'production', 'label': 'Production', 'kind': 'baseline', 'available': True,
                      'description': PRODUCTION})
        log = self.pipelines.tag_log()
        tags = {tag: entry['pipeline'] for tag, entry in self.pipelines.tags().items()}
        return {'datasets': cards, 'tags': tags,
                'history': {tag: [e for e in log if e['tag'] == tag] for tag in tags}}

    def card(self, dataset):
        value = self.view(dataset)
        record = value.get('experiment') or {}
        result = value.get('result') or {}
        summary = (value.get('comparison') or {}).get('summary')
        changes = [row for row in (value['pipelines']['diff'] or []) if row['status'] != 'unchanged']
        variables = [f'{row["path"]}: {", ".join(f["field"] for f in row.get("fields", [])) or ""}'
                     f'{" prompts " + ", ".join(p["name"] for p in row.get("prompts", [])) if row.get("prompts") else ""}'
                     .strip().rstrip(':') if row['status'] == 'changed' else f'{row["path"]} {row["status"]}'
                     for row in changes]
        label = record.get('name', 'Production') + (f' · {value["round"]}' if value.get('round') else '')
        return {'id': dataset, 'label': label, 'kind': value['kind'], 'available': True, 'status': value['status'],
                'hypothesis': record.get('hypothesis') or PRODUCTION, 'created_at': record.get('created_at'),
                'variables': variables or (['No changes: identical rerun'] if record else []),
                'round': value.get('round'),
                'cases': {'selected': len(result.get('keys', [])) or len(selected(record)), 'companions': 0},
                'impact': {k: summary[k] for k in ('verdicts', 'headline', 'flows', 'cases')} if summary else None,
                'reference': bool((value.get('comparison') or {}).get('accuracy')),
                'cost': self.cost(value)}

    def cost(self, value):
        runs = value.get('runs') or {}
        candidate = runs.get('candidate')
        if not candidate:
            return None
        spent = candidate['cost']
        return {'new_usd': spent['spent_usd'], 'reused_usd': spent['reused_value_usd'], 'events': spent['events']}

    def overview(self, dataset):
        """The view's summary; cached until its runs, experiments or tags change (quotation checks are slow)."""
        key = ('overview', dataset, self.signature())
        with self.lock:
            if key in self.summaries:
                return self.summaries[key]
        result = self._overview(dataset)
        with self.lock:
            self.summaries = {k: v for k, v in self.summaries.items() if k[2] == key[2]}
            self.summaries[key] = result
        return result

    def _overview(self, dataset):
        value = self.view(dataset)
        comparison = value.get('comparison')
        sides = []
        for side, run in (value.get('runs') or {}).items():
            values = value['sides'][side]
            sides.append({'side': side, 'name': run['name'], 'pipeline': run['pipeline'], 'kind': 'run',
                          'status_counts': dict(Counter(compare.state(v, side == 'baseline' and value.get('settled', False))
                                                        for v in values.values())),
                          'case_count': len(values),
                          'record_count': sum(len(v.get('records') or []) for v in values.values() if not is_failure(v))})
        impact = None
        if comparison:
            summary = dict(comparison['summary'])
            for side in ('baseline', 'candidate'):
                summary[side] = {**summary[side], **self.extras(value, side)}
            accuracy = comparison['accuracy']
            impact = {'groups': {'selected': summary}, 'questions': comparison['questions'],
                      'reference': bool(accuracy),
                      'accuracy': {'comparable_fields': accuracy['scored_facts'],
                                   'baseline_correct': accuracy['baseline_correct'],
                                   'candidate_correct': accuracy['candidate_correct']} if accuracy else None}
        profile = None
        if value['kind'] == 'baseline' and value['status'] == 'finalized':
            profile = compare.profile(value['sides']['baseline'].values())
            profile['metrics'].update(self.extras(value, 'baseline'))
        record, result = value.get('experiment') or {}, value.get('result') or {}
        return {'dataset': dataset, 'kind': value['kind'], 'status': value['status'], 'card': self.card(dataset),
                'experiment': {**record, 'round': value.get('round'), 'focus': record.get('focus'),
                               'selection': result.get('selection', record.get('probe')),
                               'cases': {'selected': result.get('keys', selected(record)), 'companions': []}}
                if record else None,
                'round': {k: result[k] for k in ('steps', 'regressions', 'status', 'finished_at', 'cost')} if result else None,
                'cost_summary': self.cost(value), 'impact': impact, 'profile': profile,
                'output': value.get('output'), 'runs': sides, 'pipelines': value['pipelines'],
                'tags': {tag: entry['pipeline'] for tag, entry in self.pipelines.tags().items()},
                'fact_labels': {f['key']: f['label'] for f in contract.CASE_FACTS + contract.RECORD_FACTS},
                'reference': None, 'plan': value.get('plan')}

    def extras(self, value, side):
        """Warnings from the run's trace and unverified quotations, for one side's cases."""
        values, cases = value['sides'][side], value.get('cases')
        run = (value.get('runs') or {}).get(side)
        warnings = 0
        if run and run.get('traces'):
            traces = self.datasets.open(run['traces'])
            warnings = sum(e['event'] == 'warning' for k in values if k in traces for e in traces.get(k))
        unverified = 0
        if cases is not None:
            for key, result in values.items():
                if not is_failure(result) and key in cases:
                    pages = case_contract.pages(cases.get(key)['text'])
                    unverified += len(contract.verify_quotes(result, pages)[1])
        return {'warnings': warnings, 'unverified_quotes': unverified}

    def case_row(self, key, value, effect):
        b, c = (value['sides'].get(side, {}).get(key) for side in ('baseline', 'candidate'))
        chosen = next((v for v in (c, b) if v is not None and not is_failure(v)), None)
        dockets = sorted({r['docket'] for v in (b, c) if v is not None and not is_failure(v)
                          for r in v['records'] if r['docket'] != contract.UNKNOWN})
        if not dockets and value.get('cases') is not None and key in value['cases']:
            dockets = value['cases'].get(key)['dockets']
        unknown = sum(r['duration']['kind'] == contract.UNKNOWN for r in (chosen or {}).get('records', []))
        return {'case_id': key, 'dockets': dockets,
                'disposition': ((chosen or {}).get('disposition') or {}).get('value') or 'none',
                'status': {'baseline': compare.state(b, value.get('settled', False)), 'candidate': compare.state(c) if 'candidate' in value['sides'] else None},
                'gaps': {'duration_unknown': unknown},
                'record_count': {side: (len(v['records']) if v is not None and not is_failure(v) else 0)
                                 if side in value['sides'] else None for side, v in (('baseline', b), ('candidate', c))},
                'impact': {**{k: effect[k] for k in ('verdict', 'chips', 'highlights', 'processing')},
                           'verified': any(f['verified'] in ('corrected', 'regressed') for f in effect['facts']),
                           'role': 'selected'} if effect else None}

    def cases(self, dataset, q='', status='', scope='', verdict='', role='', gap='', offset='0', limit='50'):
        offset, limit = int(offset), int(limit)
        if offset < 0 or not 1 <= limit <= 200 or len(q) > 200:
            raise ValueError('Invalid pagination or search')
        if verdict and verdict not in VERDICTS or gap and gap != 'duration_unknown':
            raise ValueError('Unknown filter')
        value = self.view(dataset)
        if value['status'] != 'finalized':
            raise ValueError('This view has no results yet')
        effects = {c['case_id']: c for c in (value.get('comparison') or {}).get('cases', [])}
        keys = sorted(set().union(*[set(v) for v in value['sides'].values()]))
        rows = []
        for key in keys:
            row = self.case_row(key, value, effects.get(key))
            if q.casefold() not in ' '.join([key, *row['dockets']]).casefold():
                continue
            if status and status not in [s for s in row['status'].values() if s] or scope and scope != row['disposition']:
                continue
            if gap and not row['gaps']['duration_unknown'] or verdict and (row['impact'] or {}).get('verdict') != verdict:
                continue
            rows.append(row)
        if effects:
            rows.sort(key=lambda r: VERDICTS.index(r['impact']['verdict']))
        return {'cases': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit}

    def detail(self, dataset, id):
        value = self.view(dataset)
        result = {'case_id': id}
        found = False
        for side in ('baseline', 'candidate'):
            item = value.get('sides', {}).get(side, {}).get(id)
            result[side] = self.shaped(value, side, id, item) if item is not None else None
            found = found or item is not None
        if not found:
            raise KeyError(id)
        effect = next((c for c in (value.get('comparison') or {}).get('cases', []) if c['case_id'] == id), None)
        result['impact'] = effect
        if effect is None and result['baseline']:
            item = value['sides']['baseline'][id]
            result['facts'] = compare.compare_case(id, item, None)['one_sided'] if not is_failure(item) else []
        reference = (value.get('reference') or {}).get(id)
        result['reference'] = reference
        return result

    def shaped(self, value, side, key, item):
        """One side's case in the shape the case view reads, with its trace."""
        run = (value.get('runs') or {}).get(side)
        trace = []
        if run and run.get('traces'):
            traces = self.datasets.open(run['traces'])
            trace = traces.get(key) if key in traces else []
        cases = value.get('cases')
        dockets = cases.get(key)['dockets'] if cases is not None and key in cases else []
        if is_failure(item):
            return {'case_id': key, 'status': {'state': 'failed', 'errors': [{'error': item.reason, 'step': item.step,
                                                                             'type': item.kind}]},
                    'records': [], 'disposition': None, 'source': {'dockets': dockets}, 'trace': trace}
        unverified = []
        if cases is not None and key in cases:
            unverified = contract.verify_quotes(item, case_contract.pages(cases.get(key)['text']))[1]
        return {**item, 'case_id': key, 'status': {'state': 'complete', 'errors': [], 'quotes': {'unverified': unverified}},
                'source': {'dockets': dockets}, 'trace': trace}

    def source(self, dataset, id, side='baseline'):
        value = self.view(dataset)
        cases = value.get('cases')
        if cases is None or id not in cases:
            raise KeyError(id)
        case = cases.get(id)
        if sha256(case['text']) != case['text_sha256']:
            raise ValueError('Case text does not match its hash; withheld')
        return {'case_id': id, 'side': side, 'text': case['text'], 'sha256': case['text_sha256'], 'verified': True}
