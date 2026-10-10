"""Execute a pipeline tree over cases.

``Context`` is the only way steps reach a model. It knows the current step's
path, builds providers, routes calls through the run's ``Calls`` (reuse,
spending cap, dry run), and records each call in the cases' traces.

``execute`` runs the tree, then turns every case into one result under the
output contract (``appeal_schema``) and a processing status beside it.
Re-executing a tree is cheap because identical calls are reused.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import appeal_schema
from pipeline import providers
from pipeline.steps import validate
from pipeline.store import BudgetRefused, DeliveryFailed, DryRunMiss, call_key


def value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class Context:
    def __init__(self, calls, workers=3, samples=None, offline=False, endpoint=None):
        self.calls, self.workers = calls, workers
        self.samples = dict(samples or {})     # step path -> sample number
        self.offline, self.endpoint = offline, dict(endpoint or {})
        self.path = ''
        self._providers, self._lock = {}, threading.Lock()

    def at(self, step):
        view = copy.copy(self)
        view.path = f'{self.path}/{step.name}' if self.path else step.name
        return view

    def provider(self, model):
        key = (model.name, model.env)
        with self._lock:
            if key not in self._providers:
                self._providers[key] = providers.build(model.name, model.spec(), offline=self.offline, **self.endpoint)
            return self._providers[key]

    def map(self, function, items):
        items = list(items)
        if self.workers <= 1 or len(items) <= 1:
            for item in items:
                function(item)
            return
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(function, items))

    def call(self, step, group, provider, request, retry=False):
        """The answer for a group's request, or None after failing or pending its targets.

        Every attempt is traced with its key and group members, including
        refused and failed ones, so batches stay visible in any outcome.
        """
        sample = self.samples.get(self.path, 0)
        members = [c.id for c in group.members] if len(group.members) > 1 else None

        def trace(**entry):
            for case in group.targets:
                case.note(self.path, event='call', key=key, sample=sample or None, members=members, **entry)
        key = call_key(provider, request, sample)
        try:
            value, key, kind = self.calls.call(provider, request, sample, retry=retry)
        except DryRunMiss:
            trace(kind='new', outcome='dry_run')
            group.pend(self.path, 'A new call is needed; a dry run does not send it')
            return None
        except BudgetRefused as exc:
            trace(kind='refused', outcome='budget')
            group.fail(self.path, exc, 'budget')
            return None
        except DeliveryFailed as exc:
            trace(kind='refused', outcome='delivery')
            group.fail(self.path, exc, 'delivery')
            return None
        except Exception as exc:  # Transport, HTTP and response errors fail this group only.
            trace(kind='new', outcome='failed')
            group.fail(self.path, exc, 'delivery')
            return None
        group.scratch['key'] = key
        trace(kind=kind, value=value_hash(value))
        return value

    def record_acceptance(self, step, group, request, rejected):
        if rejected:
            for case in group.targets:
                case.note(self.path, event='rejected', key=group.scratch.get('key'),
                          errors=[str(e) for e in rejected][:3])

    def may_retry(self, step, provider, request):
        key = call_key(provider, request, self.samples.get(self.path, 0))
        return self.calls.retry_failed and key not in self.calls.retried


def execute(root, cases, ctx):
    """Run a tree over cases; return ``{case id: (result, status, trace)}``."""
    validate(root)
    root.run(cases, ctx)
    return {case.id: conclude(case) for case in cases}


def conclude(case):
    """A case's result under the output contract, and its processing status."""
    status = case.status()
    if status['state'] == 'complete' and case.disposition is None:
        case.fail('', 'The pipeline ended without a disposition', 'contract', retryable=False)
        status = case.status()
    records = case.records or [] if status['state'] == 'complete' else []
    result = appeal_schema.result(case.id, case.disposition, records)
    problems = appeal_schema.validate(result)
    if problems:
        case.fail('', 'Result violates the output contract: '+'; '.join(problems[:5]), 'contract', retryable=False)
        status = case.status()
        result = appeal_schema.result(case.id, case.disposition if not appeal_schema.validate(
            appeal_schema.result(case.id, case.disposition)) else None)
    checked, unverified = appeal_schema.verify_quotes(result, case.pages)
    status['quotes'] = {'checked': checked, 'unverified': unverified}
    return result, status, case.trace
