"""The state a pipeline carries for each case while it runs.

A case starts active. A step may exclude it (a disposition with a reason),
finish it early (complete, with no further processing), or fail it. Selectors
only pick active cases. In a dry run, a case whose next call would be new is
left pending, because its later requests cannot be known.

A group is the unit a step works on: its members are the cases in the call's
context and its targets are the cases the call updates. A conversation
belongs to a group, so a step can continue its predecessor's conversation
only within the same group.
"""
import hashlib
import threading


class Case:
    def __init__(self, case_id, source, meta=None):
        self.id, self.source, self.meta = case_id, source, dict(meta or {})
        self.state = 'active'       # active, finished, excluded, failed or pending
        self.disposition = None
        self.values = {}            # Intermediate step outputs, by name.
        self.records = None
        self.errors, self.warnings, self.trace = [], [], []
        self._pages, self._lock = None, threading.Lock()

    @property
    def pages(self):
        if self._pages is None:
            from appeal_evidence import source_pages
            self._pages = source_pages(self.source)
        return self._pages

    @property
    def active(self):
        return self.state == 'active'

    @property
    def blocking(self):
        """Stopped by something a retry or a real run could change."""
        return self.state == 'pending' or self.state == 'failed' and any(e.get('retryable') for e in self.errors)

    def note(self, step, **entry):
        with self._lock:
            self.trace.append({'step': step, **entry})

    def set_disposition(self, step, disposition):
        before = (self.disposition or {}).get('value')
        self.disposition = disposition
        if disposition['value'] != before:
            self.note(step, event='disposition', before=before, after=disposition['value'])

    def exclude(self, step, disposition):
        """Progressive exclusion: later selectors skip this case."""
        self.set_disposition(step, {**disposition, 'value': 'excluded'})
        self.state = 'excluded'

    def finish(self, step):
        self.state = 'finished'
        self.note(step, event='finished')

    def fail(self, step, error, kind='error', retryable=True):
        self.state = 'failed'
        self.errors.append({'step': step, 'error': str(error), 'type': kind, 'retryable': retryable})
        self.note(step, event='failed', error=str(error), type=kind)

    def pend(self, step, reason):
        self.state = 'pending'
        self.note(step, event='pending', reason=reason)

    def status(self):
        state = {'active': 'complete', 'finished': 'complete', 'excluded': 'complete',
                 'failed': 'failed', 'pending': 'in_progress'}[self.state]
        return {'state': state, 'errors': self.errors, 'warnings': self.warnings}


class Group:
    def __init__(self, members, targets=None):
        self.members = list(members)
        self.targets = list(self.members if targets is None else targets)
        self.conversation = None    # (step path, provider identity, Conversation)
        self.scratch = {}           # Values steps in this group share, e.g. a raw answer.

    @property
    def id(self):
        return hashlib.sha256('|'.join(c.id for c in self.members).encode()).hexdigest()[:16]

    def drop(self, case):
        """Stop updating a case in this group (it finished, was excluded, or failed)."""
        self.targets = [c for c in self.targets if c is not case]

    def fail(self, step, error, kind='error', retryable=True, cases=None):
        for case in list(cases or self.targets):
            case.fail(step, error, kind, retryable)
            self.drop(case)

    def pend(self, step, reason):
        for case in list(self.targets):
            case.pend(step, reason)
        self.targets = []
