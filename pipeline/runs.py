"""Run directories: one pipeline applied to a set of corpus cases.

    run.json            pipeline ID and how to rebuild it, cases with source hashes, options
    ledger.jsonl        every call the run made or reused, by content key
    results/<case>.json the case's result under the output contract
    status/<case>.json  processing status: complete, failed or in progress, errors, warnings,
                        quote verification
    traces/<case>.json  how the case moved through the steps: calls with their keys,
                        disposition changes, failures
    executions.json     each execution's options, spending and outcome

Executing a run re-executes the whole tree; identical calls are reused, so a
resume pays only for work not yet done. A sealed run is final and cannot be
executed again. A converted run holds a legacy run's outputs, without traces.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import uuid

from appeal_runtime import read, save
from config import ROOT
from pipeline import registry
from pipeline.corpus import Corpus
from pipeline.engine import Context, execute
from pipeline.store import CallStore, Calls, read_ledger

RUN_VERSION = 'appeal-run-2'
RUNS = ROOT/'data/runs'
EXPERIMENT_RUNS = ROOT/'data/experiments'
STORE = ROOT/'data/calls'


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another process is using this run') from None
        yield


def create(folder, *, name, pipeline_id, case_ids, corpus, kind='pipeline', samples=None, source_run=None):
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ValueError(f'{folder} already exists')
    entries = corpus.entries
    record = {'schema_version': RUN_VERSION, 'id': uuid.uuid4().hex, 'name': name, 'kind': kind,
              'created_at': datetime.now(timezone.utc).isoformat(), 'pipeline': pipeline_id,
              'corpus': str(corpus.folder), 'samples': dict(samples or {}), 'source_run': source_run,
              'sealed_at': None,
              'cases': [{'case_id': i, 'source_sha256': entries[i]['source_sha256']} for i in corpus.ids()
                        if i in set(case_ids)]}
    if len(record['cases']) != len(set(case_ids)):
        raise ValueError('Some cases are absent from the corpus')
    folder.mkdir(parents=True, exist_ok=True)
    save(folder/'run.json', record)
    save(folder/'executions.json', [])
    return record


def record(folder):
    value = read(Path(folder)/'run.json')
    if value.get('schema_version') != RUN_VERSION:
        raise ValueError(f'{folder} is not a run directory')
    return value


def tree_for(run, experiments=None):
    """Rebuild the run's tree and confirm it is still the frozen pipeline."""
    stored = registry.load(run['pipeline'])
    tree = registry.build(stored['source'], experiments)
    rebuilt, _ = registry.identify(tree)
    if rebuilt != run['pipeline']:
        raise ValueError(f'The pipeline code or configuration changed since {run["name"]} was planned '
                         f'({rebuilt} instead of {run["pipeline"]}); plan a new run')
    return tree


def run(folder, *, tree=None, store=None, limit=0.0, multiplier=1, workers=3, retry_failed=False,
        retry_max_tokens=None, offline=False, endpoint=None, experiments=None):
    """Execute or resume a run under a cap on its new spending."""
    folder = Path(folder)
    with locked(folder):
        meta = record(folder)
        if meta['sealed_at']:
            raise ValueError(f'{meta["name"]} is sealed; plan a new run for further work')
        if meta['kind'] != 'pipeline':
            raise ValueError('Only pipeline runs can be executed')
        tree = tree or tree_for(meta, experiments)
        store = store or CallStore(STORE)
        calls = Calls(store, folder/'ledger.jsonl', run=meta['id'], limit=limit, multiplier=multiplier,
                      retry_failed=retry_failed, retry_max_tokens=retry_max_tokens)
        before = calls.spent()
        execution = {'started_at': datetime.now(timezone.utc).isoformat(), 'max_new_spend_usd': limit,
                     'price_multiplier': multiplier, 'workers': workers, 'retry_failed': retry_failed,
                     'retry_max_output_tokens': retry_max_tokens, 'code_revision': registry.git_commit()}
        cases = Corpus(meta['corpus']).cases([c['case_id'] for c in meta['cases']])
        verify_sources(meta, cases)
        ctx = Context(calls, workers=workers, samples=meta['samples'], offline=offline, endpoint=endpoint)
        try:
            outputs = execute(tree, cases, ctx)
            write(folder, outputs)
            execution['outcome'] = 'finished'
        except BaseException as exc:
            execution.update(outcome='stopped', error=str(exc) or type(exc).__name__)
            raise
        finally:
            after = calls.spent()
            execution.update(finished_at=datetime.now(timezone.utc).isoformat(), new_spend_usd=after-before,
                             cumulative_new_spend_usd=after, statuses=dict(progress(folder)['statuses']))
            save(folder/'executions.json', [*read(folder/'executions.json'), execution])
        return execution


def verify_sources(meta, cases):
    expected = {c['case_id']: c['source_sha256'] for c in meta['cases']}
    for case in cases:
        if case.meta['source_sha256'] != expected[case.id]:
            raise ValueError(f'Corpus source for {case.id} differs from the run\'s')


def dry_run(meta, tree, store, multiplier=1, workers=4):
    """Execute without sending; return the outputs, the run's calls view and the cases."""
    cases = Corpus(meta['corpus']).cases([c['case_id'] for c in meta['cases']])
    calls = Calls(store, run=meta['id'], limit=float('inf'), multiplier=multiplier, dry_run=True)
    outputs = execute(tree, cases, Context(calls, workers=workers, samples=meta['samples'], offline=True))
    return outputs, calls, cases


def write(folder, outputs):
    for case_id, (result, status, trace) in outputs.items():
        save(folder/'results'/f'{case_id}.json', result)
        save(folder/'status'/f'{case_id}.json', status)
        if trace is not None:
            save(folder/'traces'/f'{case_id}.json', trace)


def seal(folder):
    folder = Path(folder)
    with locked(folder):
        meta = record(folder)
        if not meta['sealed_at']:
            meta['sealed_at'] = datetime.now(timezone.utc).isoformat()
            save(folder/'run.json', meta)
        return meta


def load(folder, case_ids=None, traces=False):
    """A run's cases in run order: status, disposition, records and source reference."""
    folder = Path(folder)
    meta = record(folder)
    corpus = Corpus(meta['corpus'])
    wanted = None if case_ids is None else set(case_ids)
    cases = []
    for entry in meta['cases']:
        case_id = entry['case_id']
        if wanted is not None and case_id not in wanted:
            continue
        path = folder/'results'/f'{case_id}.json'
        status_path = folder/'status'/f'{case_id}.json'
        case = {'case_id': case_id,
                'source': {'path': str(corpus.source_path(case_id)), 'sha256': entry['source_sha256'],
                           'documents': corpus.entries.get(case_id, {}).get('documents', []),
                           'dockets': corpus.entries.get(case_id, {}).get('dockets', [])},
                'status': read(status_path) if status_path.exists() else {'state': 'not_run', 'errors': [], 'warnings': []}}
        result = read(path) if path.exists() else {}
        case.update(schema=result.get('schema'), disposition=result.get('disposition'), records=result.get('records', []))
        if traces:
            trace_path = folder/'traces'/f'{case_id}.json'
            case['trace'] = read(trace_path) if trace_path.exists() else None
        cases.append(case)
    if wanted is not None and len(cases) != len(wanted):
        raise ValueError('Some selected cases are absent from the run')
    return {'run': meta, 'cases': cases}


def progress(folder):
    folder = Path(folder)
    meta = record(folder)
    statuses = Counter()
    for entry in meta['cases']:
        path = folder/'status'/f'{entry["case_id"]}.json'
        statuses[read(path)['state'] if path.exists() else 'not_run'] += 1
    return {'statuses': statuses, 'cases': len(meta['cases'])}


def cost(folder, store=None):
    """New and reused spending, counting each distinct call once."""
    folder = Path(folder)
    meta = record(folder)
    if meta['kind'] == 'converted':
        return meta.get('cost') or {'new_usd': 0, 'reused_usd': None, 'recorded_usd': None}
    path = folder/'ledger.jsonl'
    entries = read_ledger(path) if path.exists() else []
    calls = Calls(store or CallStore(STORE), path if path.exists() else None, run=meta['id'])
    reused = {e['key']: e['usd'] for e in entries if e['event'] == 'reused'}
    new = {e['key'] for e in entries if e['event'] == 'new'}
    return {'new_usd': calls.spent(), 'reused_usd': sum(reused.values()), 'new_calls': len(new),
            'reused_calls': len(reused), 'retried': sum(e['event'] == 'archived' for e in entries)}


def case_cost(trace, store=None):
    """A case's share of its calls' charges; a call shared by a group is split evenly."""
    store = store or CallStore(STORE)
    total, seen = 0.0, set()
    for entry in trace or []:
        if entry.get('event') == 'call' and entry.get('outcome') != 'dry_run' and entry['key'] not in seen:
            seen.add(entry['key'])
            prefix = store.prefix(entry['key'])
            found = prefix if Path(str(prefix)+'.result.json').exists() else (store.indexed(entry['key']) or (None, None))[1]
            if found is not None:
                total += store.charge(found)/len(entry.get('members') or [1])
    return total


def find(pipeline_id, case_ids, roots=None):
    """The sealed or newest run of a pipeline that covers these cases.

    Standalone runs live in ``data/runs/NAME``; experiment runs in
    ``data/experiments/NAME/run``.
    """
    candidates = []
    paths = [p for root in roots for p in sorted(Path(root).glob('*/run.json'))] if roots is not None else \
        sorted(RUNS.glob('*/run.json')) + sorted(EXPERIMENT_RUNS.glob('*/run/run.json'))
    for path in paths:
        meta = read(path)
        if meta.get('schema_version') == RUN_VERSION and meta['pipeline'] == pipeline_id and \
                set(case_ids) <= {c['case_id'] for c in meta['cases']} and meta['kind'] == 'pipeline':
            candidates.append((bool(meta['sealed_at']), meta['created_at'], path.parent))
    if not candidates:
        raise KeyError(f'No run of {pipeline_id} covers the selected cases')
    return max(candidates)[2]
