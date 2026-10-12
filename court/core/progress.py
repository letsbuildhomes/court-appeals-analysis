"""Live progress of a run, written while it runs: ``data/runs/NAME/progress.json``.

For each step that has started: when it started and finished, how many items
it received and returned, its failures by kind, and how many items it has
touched so far (any traced event: a call, a warning, a rejection). For each
item: the last step that noted it and whether it has failed. The runner's
ledger records every call's cost as it happens; together they show a run
live. The file is rewritten at most once a second, atomically.
"""
from datetime import datetime, timezone
import threading

from court.core.datasets import is_failure
from court.core.hashing import save_json


def now():
    return datetime.now(timezone.utc).isoformat()


class Progress:
    def __init__(self, path, trace, total, interval=1.0):
        self.path, self.trace, self.total, self.interval = path, trace, total, interval
        self.steps, self.lock = {}, threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def begin(self):
        self.write()
        self.thread.start()

    def start(self, path, inputs):
        sizes = [len(v) for v in inputs.values() if isinstance(v, dict)]
        with self.lock:
            self.steps[path or '(root)'] = {'path': path, 'started_at': now(), 'finished_at': None,
                                            'inputs': max(sizes) if sizes else 0, 'outputs': None, 'failed': {}}

    def finish(self, path, output):
        failed = {}
        for value in output.values():
            if is_failure(value):
                failed[value.kind] = failed.get(value.kind, 0)+1
        with self.lock:
            step = self.steps.setdefault(path or '(root)', {'path': path, 'started_at': now(), 'inputs': None})
            step.update(finished_at=now(), outputs=len(output), failed=failed)

    def snapshot(self):
        with self.trace._lock:
            events = {key: list(items) for key, items in self.trace.events.items()}
        touched, items = {}, {}
        for key, items_events in events.items():
            seen = set()
            for event in items_events:
                if event['step'] not in seen:
                    seen.add(event['step'])
                    touched[event['step']] = touched.get(event['step'], 0)+1
            last = items_events[-1]
            items[key] = {'step': last['step'], 'event': last['event'], 'kind': last.get('kind'),
                          'failed': any(e['event'] in ('unavailable', 'rejected') for e in items_events)}
        with self.lock:
            steps = [{**s, 'touched': touched.get(s['path'], 0)} for s in self.steps.values()]
        return {'updated_at': now(), 'total': self.total, 'steps': steps, 'items': items}

    def write(self):
        save_json(self.path, self.snapshot())

    def loop(self):
        while not self.stopping.wait(self.interval):
            try:
                self.write()
            except OSError:
                pass

    def end(self):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join()
        self.write()
