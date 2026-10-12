"""Run a pipeline, or the subtree at one position, on saved datasets.

A run is ``data/runs/<name>/``:

    run.json      the pipeline ID, the position it starts at, input dataset IDs,
                  the keys it was limited to, options, status, every saved
                  output by position, the final output, item counts and cost
    ledger.jsonl  every model call it reused, sent, was charged for or would send

Running a run name again continues it: the same pipeline, position, inputs
and keys are required, and the ledger, and so the spending cap, carry on.
Finished work comes back from the caches for free.

The final output and every node marked ``save`` are written as datasets;
each item's trace is saved as a ``traces`` dataset.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re

from court import contracts
from court.config import paths
from court.core.context import Context, Services, Trace
from court.core.datasets import Datasets, Failure, is_failure
from court.core.hashing import read_json, save_json
from court.core.models import Models, ledger_summary
from court.core.pipelines import Pipelines, find
from court.core.progress import Progress

NAME = re.compile(r'[a-z0-9][a-z0-9._-]*\Z')


class RunError(Exception):
    pass


@contextmanager
def locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError(f'Run {folder.name} is in use by another process') from None
        yield


def check_items(kind, output, path):
    """Turn items that break their contract into failures."""
    checked = {}
    for key, value in output.items():
        problems = [] if is_failure(value) else contracts.check(kind, key, value)
        checked[key] = Failure('contract', '; '.join(problems[:5]), path) if problems else value
    return checked


class Runner:
    def __init__(self, pipelines=None, datasets=None, runs_root=None, store=None, cache=None, raw=None):
        self.pipelines = pipelines or Pipelines()
        self.datasets = datasets or Datasets()
        self.root = Path(runs_root or paths().runs)
        self.store, self.cache, self.raw = store, cache, raw

    def run(self, name, pipeline, inputs=None, at='', keys=None, mode='cached', limit=0.0, resend_failed=False,
            resend_max_tokens=None, resamples=0, workers=4, transport=None, note='', save_all=False, labels=None,
            clock=None):
        """Run and return the run record. ``mode`` is send, dry or cached (never send).

        ``save_all`` saves every step's output, not only the positions marked ``save``,
        so two runs can be compared step by step. ``labels`` (such as an experiment and
        round) are recorded for the tools that list runs. ``clock`` replaces the time
        calls are priced at, for tests.
        """
        if not NAME.fullmatch(name):
            raise RunError('Run names use lowercase letters, numbers, dots, dashes and underscores')
        resolved = self.pipelines.load(pipeline)
        node = find(resolved['root'], at)
        step = self.pipelines.build(resolved, at)
        opened = {n: self.datasets.open(ref) for n, ref in (inputs or {}).items()}
        expected = set(step.input_names())
        if set(opened) != expected:
            raise RunError(f'{at or "the pipeline"} takes inputs {sorted(expected)}; got {sorted(opened)}')
        for input_name, dataset in opened.items():
            wanted = (type(step).inputs or {}).get(input_name)
            if wanted and dataset.kind != wanted:
                raise RunError(f'Input {input_name} must be a {wanted} dataset; {dataset.id} is {dataset.kind}')
        keys = sorted(set(keys)) if keys else None
        if keys:
            missing = [k for k in keys if not any(k in d for d in opened.values())]
            if missing:
                raise RunError(f'{len(missing)} keys are in no input, e.g. {missing[:3]}')
        folder = self.root/name
        record = {'name': name, 'pipeline': resolved['id'], 'at': at, 'step': node['step'],
                  'inputs': {n: d.id for n, d in sorted(opened.items())}, 'keys': keys,
                  'options': {'mode': mode, 'limit_usd': limit, 'resend_failed': resend_failed,
                              'resend_max_tokens': resend_max_tokens, 'resamples': resamples, 'save_all': save_all},
                  'note': note, 'labels': dict(labels or {})}
        with locked(folder):
            previous = read_json(folder/'run.json') if (folder/'run.json').exists() else None
            if previous:
                for field in ('pipeline', 'at', 'inputs', 'keys'):
                    if previous[field] != record[field]:
                        raise RunError(f'Run {name} exists with a different {field}; choose a new name')
                record['started_at'] = previous['started_at']
                record['executions'] = previous.get('executions', 0)+1
            else:
                record['started_at'] = datetime.now(timezone.utc).isoformat()
                record['executions'] = 1
            if previous:
                record = {**{k: v for k, v in previous.items() if k in ('outputs', 'output', 'traces', 'items', 'cost')},
                          **record}
            record.update(status='running', execution_started_at=datetime.now(timezone.utc).isoformat(),
                          pid=os.getpid(), finished_at=None)
            save_json(folder/'run.json', record)
            try:
                return self._execute(folder, record, resolved, node, step, opened, keys, name, limit, mode,
                                     resend_failed, resend_max_tokens, resamples, workers, transport, save_all, clock)
            except BaseException as exc:
                record.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                              finished_at=datetime.now(timezone.utc).isoformat())
                save_json(folder/'run.json', record)
                raise

    def _execute(self, folder, record, resolved, node, step, opened, keys, name, limit, mode, resend_failed,
                 resend_max_tokens, resamples, workers, transport, save_all, clock):
        models = Models(name, folder/'ledger.jsonl', self.store, limit=limit, mode=mode,
                        resend_failed=resend_failed, resend_max_tokens=resend_max_tokens, resamples=resamples,
                        transport=transport, clock=clock)
        trace, outputs, saved = Trace(), {}, {}

        def save(path, saved_step, output):
            kind = saved_step.node.get('save') if isinstance(saved_step.node.get('save'), str) else 'output'
            output = check_items(kind, output, path)
            dataset = self.datasets.write(kind, output, producer={
                'pipeline': resolved['id'], 'position': path, 'run': name,
                'inputs': record['inputs']})
            outputs[path] = dataset.id
            saved[id(output)] = dataset.id
            return output

        maps = {n: d.load(keys) for n, d in opened.items()}
        progress = Progress(folder/'progress.json', trace, max([len(m) for m in maps.values()] or [0]))
        services = Services(models, trace, self.cache, save, workers, self.raw, save_all, progress)
        at = record['at']
        progress.begin()
        try:
            progress.start(at, maps)
            output = step.run(maps, Context(services, at, step))
            progress.finish(at, output)
        finally:
            progress.end()
        if node.get('save') or save_all:
            output = save(at, step, output)
            final = self.datasets.open(outputs[at])
        elif id(output) in saved:
            # A composite that returns a saved child's output, such as a sequence's last step.
            final = self.datasets.open(saved[id(output)])
        else:
            final = self.datasets.write('output', output, producer={
                'pipeline': resolved['id'], 'position': at, 'run': name, 'inputs': record['inputs']})
        traces = self.datasets.write('traces', trace.events, producer={'pipeline': resolved['id'], 'run': name})
        failures = {}
        for value in output.values():
            if is_failure(value):
                failures[value.kind] = failures.get(value.kind, 0)+1
        record.update(finished_at=datetime.now(timezone.utc).isoformat(), outputs=outputs, output=final.id,
                      traces=traces.id, items={'total': len(output), 'failed': failures},
                      status='stopped' if any(k in failures for k in ('not_sent', 'budget', 'delivery'))
                      else 'complete', cost=models.summary(), error=None)
        save_json(folder/'run.json', record)
        return record

    def live(self, name):
        """A run's record with its live state: running, finished or interrupted, progress and spending."""
        folder = self.root/name
        record = self.load(name)
        running = False
        if (folder/'.lock').exists():
            with (folder/'.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except BlockingIOError:
                    running = True
        state = record.get('status', 'complete')
        if state == 'running' and not running:
            state = 'interrupted'
        progress = read_json(folder/'progress.json') if (folder/'progress.json').exists() else None
        ledger = folder/'ledger.jsonl'
        entries = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()] \
            if ledger.exists() else []
        return {'record': record, 'state': state, 'running': running, 'progress': progress,
                'spending': ledger_summary(entries, record.get('execution_started_at'))}

    def load(self, name):
        path = self.root/name/'run.json'
        if not path.exists():
            raise RunError(f'No run {name}')
        return read_json(path)

    def all(self):
        found = []
        for path in sorted(self.root.glob('*/run.json')):
            try:
                record = read_json(path)
            except (OSError, ValueError):
                continue
            if str(record.get('pipeline', '')).startswith('p-') and 'executions' in record:
                found.append(record)
        return found

    def inputs_at(self, record, position):
        """The dataset IDs a run fed to a position, when it saved them."""
        if record['at'] == position:
            return record['inputs']
        resolved = self.pipelines.load(record['pipeline'])
        node = find(resolved['root'], position)
        parent = position.rsplit('/', 1)[0] if '/' in position else ''
        wiring = node.get('inputs') or {}
        found = {}
        for name, source in wiring.items():
            path = (parent+'/' if parent else '')+source if not source.startswith('input:') else None
            if path is None or path not in record.get('outputs', {}):
                return None
            found[name] = record['outputs'][path]
        return found or None

    def find_output(self, pipeline, position, inputs, keys):
        """An existing run's saved output at a position, for the same pipeline and inputs, covering the keys.

        Runs over all their inputs come first: a run limited to some keys may
        have batched them differently.
        """
        for record in sorted(self.all(), key=lambda r: (r['keys'] is None, r.get('finished_at') or ''), reverse=True):
            if record['pipeline'] != pipeline or position not in record.get('outputs', {}):
                continue
            if self.inputs_at(record, position) != inputs:
                continue
            dataset = self.datasets.open(record['outputs'][position])
            if all(k in dataset for k in keys):
                return record, dataset
        return None, None
