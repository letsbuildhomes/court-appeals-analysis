"""The reference workspace: choosing, labeling and checking the reference cases.

The workspace is ``reference/`` in the repository:

    README.md            the process, for whoever picks it up
    GUIDE.md             how to label each field
    source.json          the frozen case set every label points into, and how it was made
    profile.json         how many cases each layer and category needs
    pools/CATEGORY.json  candidates for a category, with hints, in the order to consider them
    pools/summaries/     short reader summaries of candidates
    manifest.json        the chosen cases: split, layer, category, why, status and notes
    labels/CASE.json     one case's label: its result, which facts were labeled, judgments,
                         disputed facts, questions and history

A label's ``result`` is a results-contract result under the current schema;
``labeled`` names the facts that were checked (``case``: case facts;
``records``: per docket, a list of fact keys or ``all``); a fact not named is
not scored. ``disputed`` facts were left unresolved and are not scored either.

Statuses move candidate → chosen → labeled → verified → approved. ``check``
finds every problem with the manifest and labels; ``export`` writes the
approved labels as the dataset experiments name with ``--reference``.
"""
from collections import Counter
from datetime import datetime, timezone
import random
import re

from court.config import paths
from court.contracts import cases as case_contract, results as contract
from court.core.datasets import Datasets, is_failure
from court.core.hashing import read_json, save_json
from court.core.pipelines import Pipelines
from court.core.runner import Runner

STATUSES = ('chosen', 'labeled', 'verified', 'approved')
SPLITS = ('development', 'holdout')
CONFIDENCE = ('high', 'medium', 'low')
FACT_KEYS = {f['key'] for f in contract.RECORD_FACTS}
CASE_KEYS = {f['key'] for f in contract.CASE_FACTS}
# The legacy run: its case set and results give each candidate's hints.
LEGACY = {'cases': 'd-8705e8d4cd7fee83', 'results': 'd-bff4b12fc8f0912c'}
ENFORCEMENT = re.compile(r'\b(Town|City|Village|ANR|Agency|State)\b[^,]*\bv\.?\s', re.I)
ON_RECORD = re.compile(r'on[\s-]+the[\s-]+record', re.I)
DE_NOVO = re.compile(r'de\s+novo', re.I)


class ReferenceError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Workspace:
    def __init__(self, root=None, datasets=None, pipelines=None, runner=None):
        self.root = root or paths().reference
        self.datasets = datasets or Datasets()
        self.pipelines = pipelines or Pipelines()
        self.runner = runner or Runner(self.pipelines, self.datasets)

    def read(self, name, default=None):
        path = self.root/name
        return read_json(path) if path.exists() else default

    # Freezing the source ----------------------------------------------------
    def freeze(self, definition='docket-cases'):
        """Run a pipeline's grouping from the cache and record its case set as the labels' source."""
        resolved = self.pipelines.resolve(self.pipelines.definition(definition))
        run = self.runner.run(f'reference-cases-{resolved["id"][2:10]}', resolved['id'], {}, focus='extract',
                              keys=[], save_all=True, note='The reference set\'s frozen case set')
        cases = run['outputs']['cases']
        source = {'cases': cases, 'pipeline': resolved['id'], 'definition': definition, 'run': run['name'],
                  'count': len(self.datasets.open(cases)), 'frozen_at': now()}
        current = self.read('source.json')
        if current and current['cases'] != cases and self.read('manifest.json'):
            raise ReferenceError(f'The source is already {current["cases"]}; a new case set would need relabeling')
        save_json(self.root/'source.json', source)
        return source

    def cases(self):
        source = self.read('source.json')
        if not source:
            raise ReferenceError('Freeze a source case set first (reference freeze)')
        return self.datasets.open(source['cases'])

    # Hints and pools ----------------------------------------------------------
    def hints(self):
        """Per case, what the legacy run and the case text suggest: never the answer, only where to look."""
        cases = self.cases().load()
        old_cases = self.datasets.open(LEGACY['cases']).load()
        old = self.datasets.open(LEGACY['results']).load()
        group_of = {d['sha256']: k for k, c in old_cases.items() for d in c['documents']}
        records = {}
        for key, value in old.items():
            if not is_failure(value):
                for record in value['records']:
                    records.setdefault(identity(record['docket']), []).append(record)
        found = {}
        for key, case in cases.items():
            own = old.get(key)
            if own is not None:
                legacy, source = ('failed' if is_failure(own) else own['disposition']['value']), 'same case'
                reason = '' if is_failure(own) else own['disposition']['reason']
                scope = {} if is_failure(own) else own['disposition']
            else:
                groups = sorted({group_of.get(d['sha256']) for d in case['documents']} - {None})
                values = [old.get(g) for g in groups]
                answers = sorted({'failed' if is_failure(v) else v['disposition']['value'] for v in values if v is not None})
                legacy, source = ('/'.join(answers) or 'none'), 'old group'
                reason = ' | '.join(v['disposition']['reason'] for v in values if v is not None and not is_failure(v))
                scope = next((v['disposition'] for v in values if v is not None and not is_failure(v)), {})
            docket = case['dockets'][0] if case['dockets'] else None
            mine = records.get(identity(docket), []) if docket else []
            slots = [spec['value'](r) for r in mine for spec in contract.RECORD_FACTS if spec['since'] == contract.FIRST]
            titles = [d['title'] or '' for d in case['documents']]
            year = docket_year(docket)
            found[key] = {
                'docket': docket, 'documents': len(case['documents']), 'composition': case['composition'],
                'chars': len(case['text']), 'era': era(year), 'titles': titles[:6],
                'legacy': legacy, 'legacy_source': source, 'legacy_reason': reason[:600],
                'legacy_municipal': scope.get('municipal_appeal'), 'legacy_housing': scope.get('housing'),
                'legacy_records': len(mine),
                'unknown_share': round(sum(not known(v) for v in slots)/len(slots), 2) if slots else None,
                'enforcement': any(ENFORCEMENT.search(t) for t in titles),
                'motions_only': all(re.search(r'motion|entry|order', t, re.I) for t in titles)
                and not any(re.search(r'merits|decision and order|judgment', t, re.I) for t in titles),
                'review_mentions': [name for name, pattern in (('on the record', ON_RECORD), ('de novo', DE_NOVO))
                                    if pattern.search(case['text'])],
                'documents_shared': sorted({d for doc in case['documents'] for d in doc['caption_dockets']} - {docket}),
            }
        return found

    def pools(self, seed=20261013):
        """Candidates for each category, about four times what the profile needs, in the order to consider."""
        profile = self.read('profile.json')
        if not profile:
            raise ReferenceError('No profile.json')
        hints = self.hints()
        wanted = Counter()
        for row in profile['targets']:
            wanted[row['category']] += row['count']
        rng = random.Random(seed)
        order = sorted(hints)
        rng.shuffle(order)
        sizes = sorted(hints, key=lambda k: -hints[k]['chars'])
        # An exclusion that turned on housing, for a municipal appeal whose reason talks about dwellings.
        housing = re.compile(r'dwelling|residential|housing|apartment|single-family|two-family|condominium', re.I)
        rules = {
            'random-excluded': [k for k in order if hints[k]['legacy'] == 'excluded'],
            'random-in-scope': [k for k in order if hints[k]['legacy'] == 'in_scope'],
            'random-uncertain': [k for k in order if hints[k]['legacy'] == 'uncertain'],
            'high-unknown': sorted((k for k in hints if hints[k]['legacy'] in ('in_scope', 'uncertain')
                                    and (hints[k]['unknown_share'] or 0) >= 0.3),
                                   key=lambda k: -hints[k]['unknown_share']),
            'near-miss': [k for k in order if hints[k]['legacy'] == 'excluded'
                          and hints[k]['legacy_municipal'] == 'yes' and hints[k]['legacy_housing'] == 'no'
                          and housing.search(hints[k]['legacy_reason'])],
            'shared': [k for k in order if hints[k]['composition'] == 'shared'
                       and hints[k]['legacy'] not in ('excluded', 'none')],
            'enforcement': [k for k in order if hints[k]['enforcement']],
            'interim': [k for k in order if hints[k]['motions_only']],
            'failure': [k for k in order if 'failed' in hints[k]['legacy']],
            'long': sizes,
        }
        written = {}
        for category, count in sorted(wanted.items()):
            if category not in rules:
                raise ReferenceError(f'No pool rule for {category}')
            keys = rules[category][:max(4*count, count+4)]
            save_json(self.root/'pools'/f'{category}.json',
                      {'category': category, 'needed': count, 'seed': seed, 'available': len(rules[category]),
                       'candidates': [{'case': k, **hints[k]} for k in keys]})
            written[category] = {'needed': count, 'candidates': len(keys), 'available': len(rules[category])}
        return written

    # Checking -------------------------------------------------------------------
    def check(self):
        """Every problem with the manifest and the labels, and counts by status."""
        problems = []
        profile, manifest = self.read('profile.json'), self.read('manifest.json', {'cases': []})
        cases = self.cases()
        rows = manifest['cases']
        seen = Counter(r['case'] for r in rows)
        problems += [f'{k}: chosen {n} times' for k, n in seen.items() if n > 1]
        for row in rows:
            for field in ('case', 'split', 'layer', 'category', 'status', 'reason'):
                if not row.get(field):
                    problems.append(f'{row.get("case")}: needs {field}')
            if row.get('case') not in cases:
                problems.append(f'{row.get("case")}: not in the source case set')
            if row.get('split') not in SPLITS:
                problems.append(f'{row.get("case")}: split must be one of {", ".join(SPLITS)}')
            if row.get('status') not in STATUSES:
                problems.append(f'{row.get("case")}: status must be one of {", ".join(STATUSES)}')
        if profile:
            have = Counter((r['split'], r['layer'], r['category']) for r in rows)
            for target in profile['targets']:
                key = (target['split'], target['layer'], target['category'])
                if have[key] != target['count']:
                    problems.append(f'{"/".join(key)}: {have[key]} chosen, profile wants {target["count"]}')
            hints = {r['case']: r.get('hints', {}) for r in rows}
            for field, minimum in profile.get('spread', {}).items():
                for value, n in Counter(h.get(field) for h in hints.values()).items():
                    if value is not None and n < minimum and len(rows) >= profile['total']:
                        problems.append(f'spread: {field} {value} has {n} cases, profile wants at least {minimum}')
        # Held-back cases share no document with development cases.
        documents = {split: {d['sha256'] for r in rows if r.get('split') == split and r.get('case') in cases
                             for d in cases.get(r['case'])['documents']} for split in SPLITS}
        if documents['development'] & documents['holdout']:
            problems.append(f'{len(documents["development"] & documents["holdout"])} documents are in both a '
                            f'development and a held-back case')
        for row in rows:
            if row.get('status') in STATUSES[1:]:
                problems += [f'{row["case"]}: {p}' for p in self.check_label(row['case'], cases)]
        return {'problems': problems, 'cases': len(rows),
                'statuses': dict(Counter(r.get('status') for r in rows)),
                'categories': dict(Counter(f'{r.get("split")}/{r.get("category")}' for r in rows))}

    def check_label(self, case_id, cases=None):
        """Problems with one case's label file."""
        cases = cases or self.cases()
        label = self.read(f'labels/{case_id}.json')
        if label is None:
            return ['no label file']
        problems = []
        for field in ('case', 'result', 'labeled', 'judgments', 'disputed', 'questions', 'history'):
            if field not in label:
                problems.append(f'label needs {field}')
        if problems:
            return problems
        result, case = label['result'], cases.get(case_id)
        if label['case'] != case_id or result.get('case') != case_id:
            problems.append('label and result must name the case')
        if result.get('schema') != contract.SCHEMA:
            problems.append(f'result must be {contract.SCHEMA}')
        problems += [f'result: {p}' for p in contract.validate(result)]
        records = {identity(r.get('docket')): r for r in result.get('records', [])}
        if case['dockets'] and records and set(records) != {identity(case['dockets'][0])}:
            problems.append(f'records must be for the case\'s docket {case["dockets"][0]} only')
        labeled = label['labeled']
        if not set(labeled.get('case') or []) <= CASE_KEYS:
            problems.append(f'labeled.case has unknown facts: {sorted(set(labeled["case"]) - CASE_KEYS)}')
        for docket, keys in (labeled.get('records') or {}).items():
            if identity(docket) not in records:
                problems.append(f'labeled.records names {docket}, which has no record')
            if keys != 'all' and not set(keys) <= FACT_KEYS:
                problems.append(f'labeled.records[{docket}] has unknown facts: {sorted(set(keys) - FACT_KEYS)}')
        for judgment in label['judgments']:
            if judgment.get('field') not in FACT_KEYS | CASE_KEYS or judgment.get('confidence') not in CONFIDENCE:
                problems.append(f'judgment needs a known field and a confidence: {judgment}')
        if not set(label['disputed']) <= FACT_KEYS | CASE_KEYS:
            problems.append(f'disputed has unknown facts: {sorted(set(label["disputed"]) - FACT_KEYS - CASE_KEYS)}')
        if not problems:
            _, unverified = contract.verify_quotes(result, case_contract.pages(case['text']))
            problems += [f'quotation not found on its page: {path}' for path in unverified]
        return problems

    # Export ----------------------------------------------------------------------
    def export(self, name='reference', split=None):
        """Approved labels as a reference dataset: case → {result, labeled}, disputed facts left unscored."""
        manifest = self.read('manifest.json', {'cases': []})
        items = {}
        for row in manifest['cases']:
            if row['status'] != 'approved' or split and row['split'] != split:
                continue
            label = self.read(f'labels/{row["case"]}.json')
            labeled = unscored(label['labeled'], label['disputed'], label['result'])
            items[row['case']] = {'result': label['result'], 'labeled': labeled, 'split': row['split']}
        if not items:
            raise ReferenceError('No approved labels to export')
        dataset = self.datasets.write('reference', items, producer={'source': self.read('source.json')})
        self.datasets.name(f'{name}-{split}' if split else name, dataset.id, note='Approved reference labels')
        return {'dataset': dataset.id, 'cases': len(items)}


def unscored(labeled, disputed, result):
    """``labeled`` without the disputed facts, with ``all`` spelled out where a fact is removed."""
    disputed = set(disputed)
    records = {}
    for docket, keys in (labeled.get('records') or {}).items():
        keys = sorted(FACT_KEYS) if keys == 'all' else keys
        records[docket] = [k for k in keys if k not in disputed]
    return {'case': [k for k in labeled.get('case') or [] if k not in disputed], 'records': records}


def identity(docket):
    if not isinstance(docket, str):
        return None
    m = re.search(r'(\d{1,3})\s*-\s*(\d{1,2})\s*-\s*(\d{2})\s*V|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{1,6})', docket, re.I)
    if not m:
        return ' '.join(docket.casefold().split())
    return f'{int(m[1])}-{int(m[2])}-{m[3]}' if m[1] else f'{m[4]}-{m[5].upper()}-{int(m[6])}'


def docket_year(docket):
    if not docket:
        return None
    m = re.search(r'-(\d\d) Vtec$', docket) or re.match(r'(\d\d)-[A-Z]+-', docket)
    if not m:
        return None
    year = int(m[1])
    return 2000 + year if year < 70 else 1900 + year


def era(year):
    if year is None:
        return 'unknown'
    return '1996-2004' if year <= 2004 else '2005-2014' if year <= 2014 else '2015-2026'


def known(value):
    return value not in (None, '', 'unclear', contract.UNKNOWN)
