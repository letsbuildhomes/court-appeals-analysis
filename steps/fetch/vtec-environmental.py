"""Fetch the Vermont Environmental Division's opinions listing and its PDFs.

With ``snapshot`` set, the listing pages are read from that saved snapshot
and nothing is crawled; without it, the listing is crawled into a new
snapshot (named in the trace). Each linked PDF is downloaded once: a download
is cached by its media URL and the file is kept in the raw store under its
hash, so later runs never fetch it again. ``max_pdfs`` limits how many
distinct PDFs are taken, in listing order.

Requests are paced, retried with backoff on 429 and 5xx, and halted on 403
or a very long Retry-After. A PDF must be under 100 MiB and look like a PDF.

Output: PDF hash → the documents contract (file, size and every listing entry
linking to it).
"""
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import random
import time
import uuid
from contextlib import ExitStack, contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urljoin, urlparse

from court.core.step import Step

SOURCE_URL = 'https://www.vtcourts.gov/opinions-decisions?f%5B0%5D=court_division_opinions_library_%3A3'
SOURCE_HOST = 'www.vtcourts.gov'
USER_AGENT = 'data-court-review/0.1 (research archive; sequential requests)'
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_RETRY_WAIT_SECONDS = 15 * 60
MAX_REDIRECTS = 5
MAX_PDF_BYTES = 100 * 1024 * 1024


class CrawlHalt(RuntimeError):
    pass


class RetryableResponse(Exception):
    def __init__(self, response):
        super().__init__(f'HTTP {response.status_code}')
        self.status_code = response.status_code
        self.retry_after = response.headers.get('retry-after')


def require_source_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != SOURCE_HOST:
        raise ValueError(f'Unexpected source URL: {url}')
    return url


def listing_url(page_index):
    return SOURCE_URL if page_index == 0 else f'{SOURCE_URL}&page={page_index}'


def parse_listing(html, page_url):
    """The page's entries and, on the first page, the listing's page count."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('article.views-row.media-document')
    if not rows:
        raise ValueError(f'No opinion rows found at {page_url}; site layout may have changed')
    entries = []
    for row in rows:
        anchor = row.select_one('.views-field-name a[href]')
        if anchor is None:
            raise ValueError(f'Opinion row without a PDF link at {page_url}')
        media_url = require_source_url(urljoin(page_url, anchor['href']))
        path = urlparse(media_url).path
        if not (path.startswith('/media/') or path.startswith('/sites/default/files/')):
            raise ValueError(f'Unexpected PDF-link path: {media_url}')
        title = anchor.get_text(' ', strip=True)
        if not title:
            raise ValueError(f'Opinion row without a title at {page_url}')
        date_field = row.select_one('.views-field-field-document-expiration')
        date_node = date_field.select_one('time') if date_field else None
        division_node = row.select_one('.views-field-field-document-court-division .field-content')
        docket_node = row.select_one('.views-field-field-document-number .field-content')
        entries.append({
            'media_url': media_url, 'title': title,
            'displayed_date': (date_node.get_text(' ', strip=True) if date_node else None) or None,
            'date_attribute': (date_node.get('datetime') if date_node else None) or None,
            'division': (division_node.get_text(' ', strip=True) or None) if division_node else None,
            'docket_raw': (docket_node.get_text(' ', strip=True) or None) if docket_node else None})
    last_link = soup.select_one('.pager__item--last a[href]')
    if last_link:
        values = parse_qs(urlparse(urljoin(page_url, last_link['href'])).query).get('page', [])
        if len(values) != 1 or not values[0].isdigit():
            raise ValueError(f'Cannot read last-page link at {page_url}')
        return entries, int(values[0]) + 1
    if soup.select_one('.pager__item--next a[href]'):
        raise ValueError(f'Next-page link without last-page link at {page_url}')
    return entries, None


class Pacer:
    def __init__(self, delay, jitter):
        self.delay, self.jitter, self.last = delay, jitter, None

    def wait(self):
        if self.last is not None:
            remaining = self.last + self.delay + random.uniform(0, self.jitter) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self.last = time.monotonic()


def retry_after_seconds(value):
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def retry_pause(attempt, status_code=None, retry_after=None):
    base = 60 * 2 ** (attempt - 1) if status_code == 429 else \
        15 * 2 ** (attempt - 1) if status_code in (500, 502, 503, 504) else 2 ** attempt
    seconds = max(base, retry_after_seconds(retry_after) or 0.0)
    if seconds > MAX_RETRY_WAIT_SECONDS:
        raise CrawlHalt(f'Server requested a {seconds:.0f}s retry delay; stop and resume later')
    time.sleep(seconds + random.uniform(0, 1))


def fetch_html(client, pacer, url):
    import httpx
    for attempt in range(1, MAX_ATTEMPTS + 1):
        pacer.wait()
        try:
            response = client.get(url, headers={'Accept': 'text/html'})
            if response.status_code == 403:
                raise CrawlHalt(f'HTTP 403 at {url}; stop instead of continuing the crawl')
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                retry_pause(attempt, response.status_code, response.headers.get('retry-after'))
                continue
            if response.status_code in (429, 503):
                raise CrawlHalt(f'Repeated HTTP {response.status_code} at {url}; stop and resume later')
            response.raise_for_status()
            require_source_url(str(response.url))
            if 'text/html' not in response.headers.get('content-type', '').lower():
                raise ValueError(f'Expected HTML at {url}')
            return response.content
        except httpx.RequestError as exc:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f'Could not fetch {url}: {exc}') from exc
            retry_pause(attempt)
    raise AssertionError('unreachable')


@contextmanager
def pdf_response(client, pacer, media_url):
    """Pace the media request and each same-host redirect."""
    current = media_url
    with ExitStack() as stack:
        for _ in range(MAX_REDIRECTS + 1):
            pacer.wait()
            response = stack.enter_context(client.stream('GET', current, headers={'Accept': 'application/pdf'},
                                                          follow_redirects=False))
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('location')
                if not location:
                    raise ValueError(f'Redirect without Location: {current}')
                current = require_source_url(urljoin(current, location))
                response.close()
                continue
            yield response
            return
    raise ValueError(f'Too many redirects for {media_url}')


def download(client, pacer, raw, media_url):
    """Download one PDF into the raw store; return its download record or raise."""
    import httpx
    for attempt in range(1, MAX_ATTEMPTS + 1):
        temporary = None
        try:
            with pdf_response(client, pacer, media_url) as response:
                if response.status_code == 403:
                    raise CrawlHalt(f'HTTP 403 at {media_url}; stop instead of continuing the crawl')
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableResponse(response)
                response.raise_for_status()
                final_url = require_source_url(str(response.url))
                content_type = response.headers.get('content-type', '')
                if 'text/html' in content_type.lower():
                    raise ValueError(f'PDF URL returned HTML: {final_url}')
                declared = response.headers.get('content-length')
                if declared and int(declared) > MAX_PDF_BYTES:
                    raise ValueError(f'PDF exceeds {MAX_PDF_BYTES} byte limit: {final_url}')
                digest, count, first = hashlib.sha256(), 0, bytearray()
                with raw.temporary() as handle:
                    temporary = Path(handle.name)
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        count += len(chunk)
                        if count > MAX_PDF_BYTES:
                            raise ValueError(f'PDF exceeds {MAX_PDF_BYTES} byte limit: {final_url}')
                        if len(first) < 1024:
                            first.extend(chunk[:1024 - len(first)])
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if b'%PDF-' not in first or count < 8:
                    raise ValueError(f'Response is not a PDF: {final_url}')
                sha = digest.hexdigest()
                path = raw.keep_pdf(temporary, sha)
                temporary = None
                return {'sha256': sha, 'bytes': count, 'final_url': final_url, 'content_type': content_type,
                        'etag': response.headers.get('etag'), 'last_modified': response.headers.get('last-modified'),
                        'file': path}
        except RetryableResponse as exc:
            if exc.status_code in (429, 503) and attempt == MAX_ATTEMPTS:
                raise CrawlHalt(f'Repeated HTTP {exc.status_code} at {media_url}; stop and resume later') from exc
            if attempt < MAX_ATTEMPTS:
                retry_pause(attempt, exc.status_code, exc.retry_after)
                continue
            raise
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt < MAX_ATTEMPTS and (isinstance(exc, httpx.RequestError)
                                           or exc.response.status_code in RETRYABLE_STATUS):
                retry_pause(attempt)
                continue
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise AssertionError('unreachable')


class VtecEnvironmental(Step):
    description = ('Reads the Environmental Division opinions listing (a saved snapshot, or a new crawl) and '
                   'downloads each linked PDF once into the raw store.')
    inputs = {}
    output = 'documents'
    defaults = {'snapshot': None, 'max_pdfs': None, 'delay_seconds': 1.5, 'jitter_seconds': 0.5}

    def run(self, inputs, ctx):
        import httpx
        with httpx.Client(follow_redirects=False, timeout=60, headers={'User-Agent': USER_AGENT}) as client:
            pacer = Pacer(self.settings['delay_seconds'], self.settings['jitter_seconds'])
            listings = self.listing(client, pacer, ctx)
            urls = list(dict.fromkeys(e['media_url'] for e in listings))
            if self.settings['max_pdfs'] is not None:
                urls = urls[:self.settings['max_pdfs']]
            downloads = {}
            for url in urls:
                try:
                    downloads[url] = ctx.cached({'media_url': url}, lambda: download(client, pacer, ctx.raw, url))
                except CrawlHalt:
                    raise
                except Exception as exc:
                    ctx.note('fetch', 'download_failed', url=url, reason=str(exc))
        documents = {}
        for entry in listings:
            record = downloads.get(entry['media_url'])
            if record is None:
                continue
            document = documents.setdefault(record['sha256'], {
                'sha256': record['sha256'], 'bytes': record['bytes'], 'file': record['file'], 'source': 'vtec',
                'listings': []})
            document['listings'].append({**{k: entry[k] for k in ('title', 'displayed_date', 'date_attribute',
                                                                   'division', 'docket_raw', 'media_url')},
                                         'final_url': record['final_url'], 'page_index': entry['page_index'],
                                         'ordinal': entry['ordinal']})
        return documents

    def listing(self, client, pacer, ctx):
        """Every listing entry, in listing order, from the snapshot or a new crawl."""
        snapshot = self.settings['snapshot']
        crawling = snapshot is None
        if crawling:
            snapshot = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
            ctx.note('fetch', 'snapshot', snapshot=snapshot)
        entries, index, count = [], 0, None
        while count is None or index < count:
            url = listing_url(index)
            html = None if crawling else ctx.raw.listing_page(snapshot, index)
            if html is None:
                if not crawling:
                    raise ValueError(f'Snapshot {snapshot} has no page {index}')
                html = fetch_html(client, pacer, url)
                ctx.raw.save_listing_page(snapshot, index, html)
            page, discovered = parse_listing(html, url)
            if index == 0:
                count = discovered or 1
            entries += [{**e, 'page_index': index, 'ordinal': i} for i, e in enumerate(page)]
            index += 1
        return entries
