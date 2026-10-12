"""Wrap the September 2026 data as datasets, without modifying it.

    september-documents  the archive's downloaded PDFs and their listing entries
    september-pages      the text stage's selected page text
    september-cases      the case set the production run used (its frozen sources)
    september-results    the production run's results, converted to the results contract;
                         its failed cases are failure items

Each is written once; importing again finds the same dataset IDs.
"""
import json
from pathlib import Path

from court import contracts
from court.config import ROOT
from court.contracts import documents as document_contract, pages as page_contract
from court.core.datasets import Datasets, Failure
from court.core.hashing import read_json, sha256

FULL_RUN = ROOT/'data/production-import-UazDoN/data/appeals/full-run'
ARCHIVE = ROOT/'data/archive.duckdb'
TEXT = ROOT/'data/text.duckdb'
CONVERTED = ROOT/'data/runs/production-converted'


def documents():
    import duckdb
    with duckdb.connect(str(ARCHIVE), read_only=True) as db:
        snapshot = db.execute('SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT 1').fetchone()[0]
        rows = db.execute("""
            SELECT d.sha256, d.byte_count, d.raw_path, e.title, e.displayed_date, e.date_attribute, e.division,
                   e.docket_raw, e.media_url, d.final_url, e.page_index, e.ordinal
            FROM listing_entries e JOIN downloads d ON d.media_url = e.media_url
            WHERE e.snapshot_id = ? AND d.status = 'downloaded' AND d.sha256 IS NOT NULL
            ORDER BY e.page_index, e.ordinal""", [snapshot]).fetchall()
    found = {}
    for sha, size, path, *listing in rows:
        entry = found.setdefault(sha, {'sha256': sha, 'bytes': size, 'file': path, 'source': 'vtec', 'listings': []})
        entry['listings'].append(dict(zip(document_contract.LISTING, listing)))
    return found


def pages():
    import duckdb
    with duckdb.connect(str(TEXT), read_only=True) as db:
        rows = db.execute("""
            SELECT pdf_sha256, page_number, status, source, reason, text, ocr_mean_word_confidence
            FROM current_analysis_pages ORDER BY pdf_sha256, page_number""").fetchall()
    return {page_contract.key(doc, page): {'doc': doc, 'page': page, 'status': status, 'source': source,
                                           'reason': reason, 'text': text, 'ocr_confidence': confidence}
            for doc, page, status, source, reason, text, confidence in rows}


def cases():
    manifest = read_json(FULL_RUN/'manifest.json')
    found = {}
    for group in manifest['groups']:
        text = (FULL_RUN/'sources'/f'{group["group_id"]}.txt').read_text()
        if sha256(text) != group['source_sha256']:
            raise ValueError(f'Frozen source changed: {group["group_id"]}')
        found[group['group_id']] = {
            'case': group['group_id'], 'dockets': group['dockets'], 'composition': group['composition'],
            'documents': [{'doc': d['doc'], 'sha256': d['sha'], 'title': d['title'], 'posted': d['posted'],
                           'listing_dockets': d['listing_dockets'], 'caption_dockets': d['caption_dockets'],
                           'basis': d['basis'], 'flags': d['flags'], 'pages': d['pages'], 'chars': d['chars']}
                          for d in group['documents']],
            'text': text, 'text_sha256': group['source_sha256']}
    return found


def results():
    found = {}
    for path in sorted((CONVERTED/'status').glob('*.json')):
        case = path.stem
        status = read_json(path)
        if status['state'] == 'complete':
            found[case] = read_json(CONVERTED/'results'/f'{case}.json')
        else:
            error = status['errors'][0]
            found[case] = Failure(error['type'], error['error'], 'september')
    return found


def run(store=None, log=print):
    store = store or Datasets()
    made = {}
    producer = {'pipeline': None, 'imported_from': 'September 2026 production data'}
    for name, build, kind, source in [('september-documents', documents, 'documents', str(ARCHIVE)),
                                      ('september-pages', pages, 'pages', str(TEXT)),
                                      ('september-cases', cases, 'cases', str(FULL_RUN)),
                                      ('september-results', results, 'results', str(CONVERTED))]:
        values = build()
        problems = [p for key, value in values.items() if not isinstance(value, Failure)
                    for p in contracts.check(kind, key, value)]
        if problems:
            raise ValueError(f'{name}: {len(problems)} contract problems, e.g. {problems[:3]}')
        dataset = store.write(kind, values, producer={**producer, 'source': source})
        if store.names().get(name) != dataset.id:
            store.name(name, dataset.id, note=f'Imported from {source}')
        made[name] = dataset.id
        log(f'{name}: {dataset.id} ({len(dataset)} items)')
    return made


def seed_downloads(cache=None, log=print):
    """Record the archive's downloads in the cache, so fetching the September snapshot sends nothing."""
    import duckdb
    from court.core.context import Cache
    cache = cache or Cache()
    with duckdb.connect(str(ARCHIVE), read_only=True) as db:
        rows = db.execute("""SELECT media_url, sha256, byte_count, final_url, content_type, etag, last_modified, raw_path
                             FROM downloads WHERE status = 'downloaded'""").fetchall()
    for url, sha, size, final, content_type, etag, modified, path in rows:
        cache.put({'entry': 'fetch:vtec-environmental', 'media_url': url},
                  {'sha256': sha, 'bytes': size, 'final_url': final, 'content_type': content_type, 'etag': etag,
                   'last_modified': modified, 'file': path})
    log(f'Seeded {len(rows)} downloads')


def seed_text(cache=None, log=print):
    """Record the text stage's September results in the cache, so the text steps replay them.

    Native text, usability decisions and OCR results are stored under the same
    keys the text steps compute, with the tool versions that produced them.
    """
    import collections
    import duckdb
    from court.core.context import Cache
    from court.core.hashing import value_hash
    cache = cache or Cache()
    with duckdb.connect(str(TEXT), read_only=True) as db:
        versions = dict(db.execute('SELECT stage, version FROM current_versions').fetchall())
        native = db.execute('SELECT pdf_sha256, page_number, text FROM current_native_pages '
                            'ORDER BY pdf_sha256, page_number').fetchall()
        triage = db.execute("""SELECT pdf_sha256, page_number, decision, reason, ink_ratio, image_count, large_image,
                                      render_error FROM current_page_triage ORDER BY pdf_sha256, page_number""").fetchall()
        ocr = db.execute("""SELECT pdf_sha256, page_number, status, text, alnum_count, word_count, bad_char_count,
                                   mean_word_confidence, error FROM current_ocr_pages""").fetchall()
    poppler = versions['native'].split('|', 1)[1]
    texts = collections.defaultdict(list)
    for sha, _, text in native:
        texts[sha].append(text)
    for sha, pages in texts.items():
        cache.put({'entry': 'text/native:pdftotext-layout', 'doc': sha}, {'tool': poppler, 'pages': pages})
    decisions = collections.defaultdict(list)
    for sha, _, decision, reason, ink, images, large, error in triage:
        decisions[sha].append({'decision': decision, 'reason': reason, 'ink': ink, 'images': images,
                               'large_image': large, 'render_error': error})
    settings = {'blank_ink': 0.0005, 'clear_text': 80, 'render_scale': 512, 'short_text': 20}
    for sha, pages in decisions.items():
        cache.put({'entry': 'text/usability:ink-check', 'doc': sha, 'native': value_hash(texts[sha]),
                   'settings': settings}, {'tool': poppler, 'pages': pages})
    tool = versions['ocr'].split('|')
    for sha, page, status, text, alnum, words, bad, confidence, error in ocr:
        cache.put({'entry': 'text/ocr:tesseract', 'doc': sha, 'page': page,
                   'settings': {'lang': 'eng', 'long_edge': 2200, 'psm': 3}},
                  {'tool': tool[1]+'|'+tool[2], 'status': status, 'text': text, 'alnum': alnum, 'words': words,
                   'bad': bad, 'mean_confidence': confidence, 'error': error})
    log(f'Seeded native text for {len(texts)} PDFs, usability for {len(decisions)}, OCR for {len(ocr)} pages')
