"""Selectors choose which cases a step works on and group them into calls.

A selector takes the active cases in run order and returns groups. ``requires``
names values a case must already have (for example a draft record); cases
without them are skipped. ``max_cases`` is the largest group the selector can
form, which must not exceed what the step declares it can handle.
"""
import json


class Selector:
    kind = None
    max_cases = None

    def __init__(self, requires=()):
        self.requires = tuple(requires)

    def eligible(self, cases):
        return [c for c in cases if c.active and all(c.values.get(v) is not None for v in self.requires)]

    def groups(self, cases):
        raise NotImplementedError

    def text(self):
        raise NotImplementedError

    def describe(self):
        return {'kind': self.kind, 'max_cases': self.max_cases, 'requires': list(self.requires),
                **self.params(), 'text': self.text()}

    def params(self):
        return {}


class Each(Selector):
    """One case per group."""
    kind = 'each'
    max_cases = 1

    def groups(self, cases):
        return [[c] for c in self.eligible(cases)]

    def text(self):
        return 'One case per call'


class Sequential(Selector):
    """Pack cases in run order up to a case count and a character budget.

    A case's size is its source length plus the JSON length of ``measure``, one
    of its values. This is how the first production run reviewed pairs.
    """
    kind = 'sequential'

    def __init__(self, max_cases, max_chars, measure=None, requires=()):
        super().__init__(requires)
        self.max_cases, self.max_chars, self.measure = max_cases, max_chars, measure

    def size(self, case):
        return len(case.source) + (len(json.dumps(case.values.get(self.measure))) if self.measure else 0)

    def groups(self, cases):
        result, batch, total = [], [], 0
        for case in self.eligible(cases):
            amount = self.size(case)
            if batch and (len(batch) >= self.max_cases or total+amount > self.max_chars):
                result.append(batch)
                batch, total = [], 0
            batch.append(case)
            total += amount
        if batch:
            result.append(batch)
        return result

    def params(self):
        return {'max_chars': self.max_chars, 'measure': self.measure}

    def text(self):
        measured = f' (source plus {self.measure})' if self.measure else ''
        return f'Up to {self.max_cases} cases per call, in run order, within {self.max_chars:,} characters{measured}'


def each(requires=()):
    return Each(requires)


def sequential(max_cases, max_chars, measure=None, requires=()):
    return Sequential(max_cases, max_chars, measure, requires)
