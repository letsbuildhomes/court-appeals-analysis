"""The grouping report and the case-set diff.

The report lists what a person should look at to trust the grouping: flags
by kind with the documents involved, documents whose dockets came only from
the listing or from nowhere, unusually large groups, and, when a triage
output is given, documents the model said do not belong to their case.

The diff compares two case sets by document membership: which cases split,
merged, gained or lost documents, changed only their text, or stayed
identical. Only changed cases need new extraction.
"""
from collections import Counter, defaultdict


def report(cases, triage=None, large=8):
    documents = [(key, d) for key, case in cases.items() for d in case['documents']]
    flags = defaultdict(list)
    for key, d in documents:
        for flag in d['flags']:
            kind, _, docket = flag.partition(':')
            flags[kind].append({'case': key, 'doc': d['doc'], 'docket': docket, 'title': d['title']})
    weak = [{'case': key, 'doc': d['doc'], 'basis': d['basis'], 'title': d['title']} for key, d in documents
            if d['basis'] in ('listing_only', 'none', 'disagree')]
    sizes = sorted(((len(c['documents']), key) for key, c in cases.items()), reverse=True)
    membership = []
    for key, value in (triage or {}).items():
        if isinstance(value, dict) and 'triage' in value:
            for item in value['triage'].get('documents', []):
                if isinstance(item, dict) and item.get('belongs') in ('no', 'unclear'):
                    membership.append({'case': key, 'doc': item.get('doc'), 'belongs': item['belongs']})
    return {'cases': len(cases), 'documents': len(documents),
            'multi_docket_cases': sum(len(c['dockets']) > 1 for c in cases.values()),
            'composition': dict(Counter(c['composition'] for c in cases.values())),
            'basis': dict(Counter(d['basis'] for _, d in documents)),
            'flags': {kind: {'count': len(items), 'documents': items} for kind, items in sorted(flags.items())},
            'weak_documents': weak,
            'largest_cases': [{'case': key, 'documents': n} for n, key in sizes[:10] if n >= large],
            'membership_doubts': membership}


def diff(before, after):
    """How documents moved between two case sets."""
    where = {side: {d['sha256']: key for key, case in cases.items() for d in case['documents']}
             for side, cases in (('before', before), ('after', after))}
    members = {side: {key: {d['sha256'] for d in case['documents']} for key, case in cases.items()}
               for side, cases in (('before', before), ('after', after))}
    split = {key: sorted({where['after'].get(sha) for sha in docs})
             for key, docs in members['before'].items() if len({where['after'].get(sha) for sha in docs}) > 1}
    merged = {key: sorted({where['before'].get(sha) for sha in docs})
              for key, docs in members['after'].items() if len({where['before'].get(sha) for sha in docs}) > 1}
    same_ids = set(before) & set(after)
    regrouped = {k for k in same_ids if members['before'][k] != members['after'][k]}
    text_only = {k for k in same_ids - regrouped if before[k]['text_sha256'] != after[k]['text_sha256']}
    return {'identical': sum(before[k] == after[k] for k in same_ids),
            'text_changed': sorted(text_only),
            'documents_changed': {k: {'gained': len(members['after'][k] - members['before'][k]),
                                      'lost': len(members['before'][k] - members['after'][k])} for k in sorted(regrouped)},
            'split': split, 'merged': merged,
            'removed': sorted(set(before) - set(after)), 'added': sorted(set(after) - set(before))}
