"""The text audit: pages without usable text, by document."""
from collections import Counter, defaultdict

from court.core.datasets import is_failure


def audit(pages):
    by_doc = defaultdict(list)
    failures = []
    for key, page in pages.items():
        if is_failure(page):
            failures.append(key)
        else:
            by_doc[page['doc']].append(page)
    flagged = []
    for doc, values in sorted(by_doc.items()):
        unresolved = [p['page'] for p in values if p['status'] == 'unresolved']
        blank = [p['page'] for p in values if p['status'] == 'blank']
        if unresolved:
            flagged.append({'doc': doc, 'pages': len(values), 'unresolved': unresolved, 'blank': blank})
    values = [p for v in by_doc.values() for p in v]
    return {'documents': len(by_doc), 'pages': len(values),
            'status': dict(Counter(p['status'] for p in values)),
            'source': dict(Counter(p['source'] or 'none' for p in values)),
            'reasons': dict(Counter(p['reason'] for p in values)),
            'documents_with_unresolved_pages': flagged, 'failures': failures}
