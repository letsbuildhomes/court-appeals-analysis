"""Case sets: case ID → one docket group's documents and full text.

    case          the case ID, also the item key, e.g. g-1-1-09-vtec
    dockets       normalized docket numbers, in filing order
    composition   single, uniform (several documents, one docket set) or linked
    documents     in case order: doc (1-based number used by citations), sha256, title,
                  posted, listing_dockets, caption_dockets, basis, flags, pages, chars
    text          the case text: every document's pages, marked as shown below
    text_sha256   hash of the text

The text format is part of the contract, because citations point into it::

    <group documents="2">
    <document doc="1" basis="agree" caption_dockets="..." listing_dockets="..." flags="...">
    <page number="1" status="usable" source="native">
    ...escaped page text...
    </page>
    </document>
    </group>
"""
import hashlib
import re

KIND = 'cases'
VERSION = 'cases-1'
FIELDS = {'case', 'dockets', 'composition', 'documents', 'text', 'text_sha256'}


def check(key, value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        return [f'{key}: needs {", ".join(sorted(FIELDS))}']
    problems = []
    if value['case'] != key:
        problems.append(f'{key}: key must be the case ID')
    if hashlib.sha256(value['text'].encode()).hexdigest() != value['text_sha256']:
        problems.append(f'{key}: text does not match its hash')
    return problems


def pages(text):
    """``{doc: {page: text}}`` parsed from a case text, for checking citations."""
    return {int(doc): {int(page): body for page, body in re.findall(
        r'<page number="(\d+)"[^>]*>(.*?)</page>', document, re.S)}
        for doc, document in re.findall(r'<document doc="(\d+)"[^>]*>(.*?)</document>', text, re.S)}
