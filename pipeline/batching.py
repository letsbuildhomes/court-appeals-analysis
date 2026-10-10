"""Batchers group cases into calls for batched phases.

Batching is a recipe setting, so an experiment can change it. ``sequential``
packs cases in run order up to a case count and a character budget; this is
how the first production run reviewed pairs. ``single`` gives every case its
own call.
"""
import json


def _size(case):
    return len(case['source']) + len(json.dumps(case.get('candidate')))


def sequential(cases, max_cases, max_chars):
    batch, total = [], 0
    for case in cases:
        amount = _size(case)
        if batch and (len(batch) >= max_cases or total+amount > max_chars):
            yield batch
            batch, total = [], 0
        batch.append(case)
        total += amount
    if batch:
        yield batch


BATCHERS = {
    'sequential': lambda cases, config: sequential(cases, config.get('max_cases', 2), config.get('max_chars', 200000)),
    'single': lambda cases, config: ([case] for case in cases),
}


def batches(cases, config):
    kind = config.get('kind', 'sequential')
    if kind not in BATCHERS:
        raise ValueError(f'Unknown batcher {kind!r}; choose from {", ".join(sorted(BATCHERS))}')
    return list(BATCHERS[kind](cases, config))


def describe(config):
    if config.get('kind') == 'single':
        return 'One case per call'
    return (f'Up to {config.get("max_cases", 2)} cases per call, in run order, '
            f'within {config.get("max_chars", 200000):,} characters')
