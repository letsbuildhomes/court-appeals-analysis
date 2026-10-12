"""Documents: PDF hash → the file and where it was listed.

    sha256     the PDF's SHA-256, also the item key
    bytes      file size
    file       the PDF in the blob store, relative to the repository (in/pdfs/<sha256>.pdf)
    source     which fetch source found it, e.g. "vtec"
    listings   every listing entry linking to it, in listing order:
               title, displayed_date, date_attribute, division, docket_raw, media_url,
               final_url, page_index, ordinal
"""
import re

KIND = 'documents'
VERSION = 'documents-1'
SHA = re.compile(r'[0-9a-f]{64}\Z')
LISTING = ('title', 'displayed_date', 'date_attribute', 'division', 'docket_raw', 'media_url', 'final_url',
           'page_index', 'ordinal')


def check(key, value):
    problems = []
    if not isinstance(value, dict) or set(value) != {'sha256', 'bytes', 'file', 'source', 'listings'}:
        return [f'{key}: needs sha256, bytes, file, source and listings']
    if value['sha256'] != key or not SHA.match(key):
        problems.append(f'{key}: key must be the PDF hash')
    if not isinstance(value['listings'], list) or not all(isinstance(e, dict) and set(e) == set(LISTING)
                                                         for e in value['listings']):
        problems.append(f'{key}: malformed listings')
    return problems
