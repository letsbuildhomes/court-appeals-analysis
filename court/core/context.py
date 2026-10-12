"""The context every step runs with, and the services it carries.

A step reaches the outside world only through its context:

    ctx.run(child, inputs)       run a child step; saves its output if its node says so, and
                                 limits its inputs if it is the run's focus (see ``Focus``)
    ctx.map(function, items)     run a function over items in parallel
    ctx.ask(keys, settings, request, accept)
                                 call a model for the items ``keys`` (see ``models.Models.ask``)
    ctx.provider(settings)       the provider for model settings, to render requests
    ctx.note(keys, event, ...)   add to the trace of each item
    ctx.cached(parts, compute)   reuse expensive code work saved under a key
    ctx.raw                      the raw input store: PDFs by hash and saved listing pages

Every service can be replaced, which is how tests and practice runs work.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from court.config import paths
from court.core.datasets import Failure, is_failure, item_hash
from court.core.hashing import read_json, save_json, value_hash
from court.core.models import Rejected, Unavailable


class Trace:
    """What happened to each item: steps, calls and outcomes."""

    def __init__(self):
        self.events, self._lock = {}, threading.Lock()

    def note(self, item, path, event, **data):
        with self._lock:
            self.events.setdefault(item, []).append({'step': path, 'event': event,
                                                    **{k: v for k, v in data.items() if v is not None}})


class Cache:
    """Per-item results of expensive code work, keyed by the step and its input."""

    def __init__(self, root=None):
        self.root = Path(root or paths().cache)

    def path(self, digest):
        return self.root/digest[:2]/f'{digest}.json'

    def get(self, parts):
        path = self.path(value_hash(parts))
        return read_json(path)['value'] if path.exists() else None

    def put(self, parts, value):
        save_json(self.path(value_hash(parts)), {'parts': parts, 'value': value})


class Focus:
    """Limits the inputs of the step at one position to chosen keys.

    Steps before that position run on everything. The chosen keys are ``keys``
    plus, with ``changed_from`` (input name to an index ``{key: item hash}`` of
    another run's input at the same position), every key whose item differs from
    or is missing in that index. ``removed`` lists the index's keys that are no
    longer in the input.
    """

    def __init__(self, path, keys=None, changed_from=None):
        self.path, self.keys, self.changed_from = path, set(keys or ()), changed_from
        self.chosen, self.removed = set(), set()

    def limit(self, inputs):
        present = set().union(*[set(values) for values in inputs.values()]) if inputs else set()
        chosen = self.keys & present
        for name, index in (self.changed_from or {}).items():
            values = inputs.get(name, {})
            chosen |= {k for k, v in values.items() if index.get(k) != item_hash(v)}
            self.removed |= set(index) - set(values)
        self.chosen |= chosen
        return {name: {k: v for k, v in values.items() if k in chosen} for name, values in inputs.items()}


class Services:
    def __init__(self, models, trace=None, cache=None, save=None, workers=4, raw=None, save_all=False, progress=None,
                 focus=None):
        from court.core.raw import Raw
        self.models, self.trace, self.cache = models, trace or Trace(), cache or Cache()
        self.save, self.workers, self.raw, self.save_all = save, workers, raw or Raw(), save_all
        self.progress, self.focus = progress, focus


class Context:
    def __init__(self, services, path='', step=None):
        self.services, self.path, self.step = services, path, step

    @property
    def raw(self):
        return self.services.raw

    def at(self, step):
        return Context(self.services, f'{self.path}/{step.name}' if self.path else step.name, step)

    def run(self, step, inputs):
        """Run a child step on input maps and return its output map."""
        ctx = self.at(step)
        if self.services.focus and self.services.focus.path == ctx.path:
            inputs = self.services.focus.limit(inputs)
        if self.services.progress:
            self.services.progress.start(ctx.path, inputs)
        output = step.run(inputs, ctx)
        if not isinstance(output, dict):
            raise TypeError(f'{ctx.path}: a step must return a map')
        if (step.node.get('save') or self.services.save_all) and self.services.save:
            output = self.services.save(ctx.path, step, output)
        if self.services.progress:
            self.services.progress.finish(ctx.path, output)
        return output

    def map(self, function, items):
        items = list(items)
        if self.services.workers <= 1 or len(items) <= 1:
            return [function(item) for item in items]
        with ThreadPoolExecutor(max_workers=self.services.workers) as pool:
            return list(pool.map(function, items))

    def note(self, keys, event, **data):
        for item in [keys] if isinstance(keys, str) else keys:
            self.services.trace.note(item, self.path, event, **data)

    def provider(self, settings):
        return self.services.models.provider(settings)

    def ask(self, keys, settings, request, accept):
        """``accept(value)``'s result for a model call, or a ``Failure`` explaining why there is none."""
        provider = self.provider(settings)

        def attempt(key, kind, error):
            self.note(keys, 'call', call=key, kind=kind, rejected=error, batch=list(keys) if len(keys) > 1 else None)
        try:
            accepted, _, _ = self.services.models.ask(provider, request, accept, attempt, step=self.path)
            return accepted
        except (Unavailable, Rejected) as exc:
            self.note(keys, 'unavailable' if isinstance(exc, Unavailable) else 'rejected', reason=str(exc),
                      kind=exc.kind)
            return Failure(exc.kind, exc, self.path)

    def cached(self, parts, compute):
        """``compute()``, reused when the same parts were computed before."""
        parts = {'entry': self.step.node['step'] if self.step else None, **parts}
        found = self.services.cache.get(parts)
        if found is not None:
            return found
        value = compute()
        self.services.cache.put(parts, value)
        return value


def fail_all(keys, failure):
    return {key: failure for key in keys}


__all__ = ['Context', 'Services', 'Focus', 'Trace', 'Cache', 'Failure', 'is_failure', 'fail_all']
