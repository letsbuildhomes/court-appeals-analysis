"""Pages: "<PDF hash>/<page number>" → the page's selected text.

    doc         the PDF's hash
    page        page number, from 1
    status      usable, unresolved or blank
    source      native or ocr for a usable page, otherwise null
    reason      why this text was chosen
    text        the selected text; null unless usable
    ocr_confidence  mean OCR word confidence when the text is OCR, otherwise null
"""
KIND = 'pages'
VERSION = 'pages-1'
FIELDS = {'doc', 'page', 'status', 'source', 'reason', 'text', 'ocr_confidence'}


def key(doc, page):
    return f'{doc}/{page}'


def check(key_, value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        return [f'{key_}: needs {", ".join(sorted(FIELDS))}']
    problems = []
    if key_ != key(value['doc'], value['page']):
        problems.append(f'{key_}: key must be doc/page')
    if value['status'] not in ('usable', 'unresolved', 'blank'):
        problems.append(f'{key_}: invalid status')
    if (value['status'] == 'usable') != isinstance(value['text'], str):
        problems.append(f'{key_}: only usable pages have text')
    return problems


def by_document(pages):
    """``{doc: [page values in page order]}`` from a pages map."""
    grouped = {}
    for value in pages.values():
        grouped.setdefault(value['doc'], []).append(value)
    for values in grouped.values():
        values.sort(key=lambda v: v['page'])
    return grouped
