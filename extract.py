"""Snapshot the Environmental opinions listing and archive its PDFs.

The listing is used only to discover PDFs and retain an audit trail. PDF bytes are
immutable raw inputs. No housing classification or case inference occurs here.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import random
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import duckdb
import httpx
from bs4 import BeautifulSoup

from config import (
    BULK_REQUEST_DELAY_SECONDS,
    BULK_REQUEST_JITTER_SECONDS,
    DB_PATH,
    DEFAULT_PDF_LIMIT,
    LISTINGS_DIR,
    MAX_PDF_BYTES,
    PDF_DIR,
    REQUEST_DELAY_SECONDS,
    ROOT,
    SOURCE_HOST,
    SOURCE_URL,
    USER_AGENT,
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_RETRY_WAIT_SECONDS = 15 * 60
MAX_REDIRECTS = 5
MAX_CONSECUTIVE_FAILURES = 3


class CrawlHalt(RuntimeError):
    """Stop the whole crawl when the server rejects or delays bulk access."""


class RetryableResponse(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.retry_after = response.headers.get("retry-after")
        super().__init__(f"HTTP {self.status_code}")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != SOURCE_HOST:
        raise ValueError(f"Unexpected source URL: {url}")
    return url


def listing_url(page_index: int) -> str:
    if page_index == 0:
        return SOURCE_URL
    return f"{SOURCE_URL}&page={page_index}"


@dataclass(frozen=True)
class ListingEntry:
    media_url: str
    title: str
    displayed_date: str | None
    date_attribute: str | None
    division: str | None
    docket_raw: str | None


def parse_listing(html: bytes, page_url: str) -> tuple[list[ListingEntry], int | None]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("article.views-row.media-document")
    if not rows:
        raise ValueError(f"No opinion rows found at {page_url}; site layout may have changed")

    entries: list[ListingEntry] = []
    for row in rows:
        anchor = row.select_one(".views-field-name a[href]")
        if anchor is None:
            raise ValueError(f"Opinion row without a PDF link at {page_url}")
        media_url = require_source_url(urljoin(page_url, anchor["href"]))
        path = urlparse(media_url).path
        if not (path.startswith("/media/") or path.startswith("/sites/default/files/")):
            raise ValueError(f"Unexpected PDF-link path: {media_url}")
        title = anchor.get_text(" ", strip=True)
        if not title:
            raise ValueError(f"Opinion row without a title at {page_url}")

        date_field = row.select_one(".views-field-field-document-expiration")
        date_node = date_field.select_one("time") if date_field else None
        displayed_date = date_node.get_text(" ", strip=True) if date_node else None
        date_attribute = date_node.get("datetime") if date_node else None
        division_node = row.select_one(
            ".views-field-field-document-court-division .field-content"
        )
        docket_node = row.select_one(
            ".views-field-field-document-number .field-content"
        )
        entries.append(
            ListingEntry(
                media_url=media_url,
                title=title,
                displayed_date=displayed_date or None,
                date_attribute=date_attribute or None,
                division=division_node.get_text(" ", strip=True) or None
                if division_node else None,
                docket_raw=docket_node.get_text(" ", strip=True) or None
                if docket_node else None,
            )
        )

    last_link = soup.select_one(".pager__item--last a[href]")
    if last_link:
        page_values = parse_qs(urlparse(urljoin(page_url, last_link["href"])).query).get(
            "page", []
        )
        if len(page_values) != 1 or not page_values[0].isdigit():
            raise ValueError(f"Cannot read last-page link at {page_url}")
        return entries, int(page_values[0]) + 1
    if soup.select_one(".pager__item--next a[href]"):
        raise ValueError(f"Next-page link without last-page link at {page_url}")
    return entries, None


class RateLimiter:
    def __init__(self, delay_seconds: float, jitter_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.jitter_seconds = jitter_seconds
        self.last_started: float | None = None

    def wait(self) -> None:
        if self.last_started is not None:
            spacing = self.delay_seconds + random.uniform(0, self.jitter_seconds)
            remaining = self.last_started + spacing - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self.last_started = time.monotonic()


def retry_after_seconds(value: str | None) -> float | None:
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


def retry_pause(attempt: int, status_code: int | None = None,
                retry_after: str | None = None) -> None:
    if status_code == 429:
        base = 60 * 2 ** (attempt - 1)
    elif status_code in (500, 502, 503, 504):
        base = 15 * 2 ** (attempt - 1)
    else:
        base = 2 ** attempt
    seconds = max(base, retry_after_seconds(retry_after) or 0.0)
    if seconds > MAX_RETRY_WAIT_SECONDS:
        raise CrawlHalt(
            f"Server requested a {seconds:.0f}s retry delay; stop and resume later"
        )
    seconds += random.uniform(0, 1)
    print(f"  pause {seconds:.0f}s before retry", flush=True)
    time.sleep(seconds)


def fetch_html(client: httpx.Client, limiter: RateLimiter, url: str) -> bytes:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        limiter.wait()
        try:
            response = client.get(url, headers={"Accept": "text/html"})
            if response.status_code == 403:
                raise CrawlHalt(f"HTTP 403 at {url}; stop instead of continuing the crawl")
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                print(f"  retry HTML {response.status_code}: {url}", flush=True)
                retry_pause(attempt, response.status_code, response.headers.get("retry-after"))
                continue
            if response.status_code in (429, 503):
                raise CrawlHalt(
                    f"Repeated HTTP {response.status_code} at {url}; stop and resume later"
                )
            response.raise_for_status()
            require_source_url(str(response.url))
            if "text/html" not in response.headers.get("content-type", "").lower():
                raise ValueError(f"Expected HTML at {url}")
            return response.content
        except httpx.RequestError as exc:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
            print(f"  retry HTML transport error: {url}", flush=True)
            retry_pause(attempt)
    raise AssertionError("unreachable")


def connect_db() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id VARCHAR PRIMARY KEY,
            source_url VARCHAR NOT NULL,
            started_at VARCHAR NOT NULL,
            completed_at VARCHAR,
            page_count INTEGER,
            status VARCHAR NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_pages (
            snapshot_id VARCHAR NOT NULL,
            page_index INTEGER NOT NULL,
            page_url VARCHAR NOT NULL,
            fetched_at VARCHAR NOT NULL,
            sha256 VARCHAR NOT NULL,
            raw_path VARCHAR NOT NULL,
            entry_count INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, page_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_entries (
            snapshot_id VARCHAR NOT NULL,
            page_index INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            media_url VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            displayed_date VARCHAR,
            date_attribute VARCHAR,
            division VARCHAR,
            docket_raw VARCHAR,
            PRIMARY KEY (snapshot_id, page_index, ordinal)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            media_url VARCHAR PRIMARY KEY,
            final_url VARCHAR,
            downloaded_at VARCHAR,
            sha256 VARCHAR,
            byte_count BIGINT,
            content_type VARCHAR,
            etag VARCHAR,
            last_modified VARCHAR,
            raw_path VARCHAR,
            status VARCHAR NOT NULL,
            error VARCHAR
        )
    """)
    return conn


def active_snapshot(conn: duckdb.DuckDBPyConnection, refresh: bool) -> tuple[str, int | None]:
    if not refresh:
        row = conn.execute(
            "SELECT snapshot_id, page_count FROM snapshots ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            return row[0], row[1]
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, NULL, NULL, 'in_progress')",
        [snapshot_id, SOURCE_URL, now_utc()],
    )
    return snapshot_id, None


def cached_page_is_valid(conn: duckdb.DuckDBPyConnection, snapshot_id: str, page_index: int) -> bool:
    row = conn.execute(
        "SELECT raw_path, sha256, entry_count FROM listing_pages "
        "WHERE snapshot_id = ? AND page_index = ?",
        [snapshot_id, page_index],
    ).fetchone()
    if not row:
        return False
    path = ROOT / row[0]
    return path.is_file() and sha256_file(path) == row[1] and row[2] > 0


def store_page(
    conn: duckdb.DuckDBPyConnection,
    snapshot_id: str,
    page_index: int,
    html: bytes,
    entries: list[ListingEntry],
) -> None:
    folder = LISTINGS_DIR / snapshot_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"page_{page_index:04d}.html"
    temporary = path.with_suffix(".html.part")
    temporary.write_bytes(html)
    os.replace(temporary, path)
    raw_path = path.relative_to(ROOT).as_posix()

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            "DELETE FROM listing_entries WHERE snapshot_id = ? AND page_index = ?",
            [snapshot_id, page_index],
        )
        conn.execute(
            "DELETE FROM listing_pages WHERE snapshot_id = ? AND page_index = ?",
            [snapshot_id, page_index],
        )
        conn.execute(
            "INSERT INTO listing_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [snapshot_id, page_index, listing_url(page_index), now_utc(),
             sha256_bytes(html), raw_path, len(entries)],
        )
        conn.executemany(
            "INSERT INTO listing_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [snapshot_id, page_index, ordinal, item.media_url, item.title,
                 item.displayed_date, item.date_attribute, item.division, item.docket_raw]
                for ordinal, item in enumerate(entries)
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def snapshot_listings(
    conn: duckdb.DuckDBPyConnection,
    client: httpx.Client,
    limiter: RateLimiter,
    snapshot_id: str,
    known_page_count: int | None,
) -> tuple[int, int, int]:
    if known_page_count is None:
        html = fetch_html(client, limiter, listing_url(0))
        entries, discovered_count = parse_listing(html, listing_url(0))
        page_count = discovered_count or 1
        store_page(conn, snapshot_id, 0, html, entries)
        conn.execute(
            "UPDATE snapshots SET page_count = ? WHERE snapshot_id = ?",
            [page_count, snapshot_id],
        )
        print(f"Listing has {page_count} pages; page 1 has {len(entries)} postings.", flush=True)
        start_index = 1
        fetched = 1
    else:
        page_count = known_page_count
        start_index = 0
        fetched = 0

    for page_index in range(start_index, page_count):
        if cached_page_is_valid(conn, snapshot_id, page_index):
            continue
        url = listing_url(page_index)
        html = fetch_html(client, limiter, url)
        entries, _ = parse_listing(html, url)
        store_page(conn, snapshot_id, page_index, html, entries)
        fetched += 1
        if fetched % 10 == 0 or page_index == page_count - 1:
            print(f"  snapshotted {fetched} new pages (through page {page_index + 1}/{page_count})", flush=True)

    actual_pages = conn.execute(
        "SELECT count(*) FROM listing_pages WHERE snapshot_id = ?", [snapshot_id]
    ).fetchone()[0]
    if actual_pages != page_count:
        raise RuntimeError(f"Snapshot incomplete: {actual_pages}/{page_count} pages")
    conn.execute(
        "UPDATE snapshots SET status = 'complete', completed_at = ? WHERE snapshot_id = ?",
        [now_utc(), snapshot_id],
    )
    postings = conn.execute(
        "SELECT count(*) FROM listing_entries WHERE snapshot_id = ?", [snapshot_id]
    ).fetchone()[0]
    distinct = conn.execute(
        "SELECT count(DISTINCT media_url) FROM listing_entries WHERE snapshot_id = ?",
        [snapshot_id],
    ).fetchone()[0]
    print(f"Snapshot {snapshot_id}: {actual_pages} pages, {postings} postings, "
          f"{distinct} unique PDF links.", flush=True)
    return postings, distinct, fetched


def selected_pdfs(
    conn: duckdb.DuckDBPyConnection, snapshot_id: str, limit: int | None
) -> list[tuple[str, str]]:
    sql = """
        SELECT media_url, title FROM (
            SELECT media_url, title, page_index, ordinal,
                   row_number() OVER (
                       PARTITION BY media_url ORDER BY page_index, ordinal
                   ) AS rank
            FROM listing_entries WHERE snapshot_id = ?
        ) WHERE rank = 1
        ORDER BY page_index, ordinal
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, [snapshot_id]).fetchall()


def cached_pdf_is_valid(conn: duckdb.DuckDBPyConnection, media_url: str) -> bool:
    row = conn.execute(
        "SELECT raw_path, sha256, byte_count FROM downloads "
        "WHERE media_url = ? AND status = 'downloaded'",
        [media_url],
    ).fetchone()
    if not row or not row[0]:
        return False
    path = ROOT / row[0]
    if not path.is_file() or path.stat().st_size != row[2]:
        return False
    return sha256_file(path) == row[1]


def record_download(
    conn: duckdb.DuckDBPyConnection,
    media_url: str,
    *,
    status: str,
    final_url: str | None = None,
    sha256: str | None = None,
    byte_count: int | None = None,
    content_type: str | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    raw_path: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM downloads WHERE media_url = ?", [media_url])
        conn.execute(
            "INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [media_url, final_url, now_utc(), sha256, byte_count, content_type,
             etag, last_modified, raw_path, status, error],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def paced_pdf_response(
    client: httpx.Client, limiter: RateLimiter, media_url: str
):
    """Pace both the media request and each same-host redirect request."""
    current_url = media_url
    with ExitStack() as stack:
        for _ in range(MAX_REDIRECTS + 1):
            limiter.wait()
            response = stack.enter_context(
                client.stream(
                    "GET", current_url, headers={"Accept": "application/pdf"},
                    follow_redirects=False,
                )
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ValueError(f"Redirect without Location: {current_url}")
                current_url = require_source_url(urljoin(current_url, location))
                response.close()
                continue
            yield response
            return
    raise ValueError(f"Too many redirects for {media_url}")


def fetch_pdf(
    conn: duckdb.DuckDBPyConnection,
    client: httpx.Client,
    limiter: RateLimiter,
    media_url: str,
    *,
    refetch: bool,
) -> str:
    if not refetch and cached_pdf_is_valid(conn, media_url):
        return "cached"

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        temporary_path: Path | None = None
        try:
            with paced_pdf_response(client, limiter, media_url) as response:
                if response.status_code == 403:
                    raise CrawlHalt(
                        f"HTTP 403 at {media_url}; stop instead of continuing the crawl"
                    )
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableResponse(response)
                response.raise_for_status()
                final_url = require_source_url(str(response.url))
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type.lower():
                    raise ValueError(f"PDF URL returned HTML: {final_url}")
                declared_size = response.headers.get("content-length")
                if declared_size and int(declared_size) > MAX_PDF_BYTES:
                    raise ValueError(f"PDF exceeds {MAX_PDF_BYTES} byte limit: {final_url}")

                digest = hashlib.sha256()
                count = 0
                first_bytes = bytearray()
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=PDF_DIR, prefix="download-", suffix=".part", delete=False
                ) as handle:
                    temporary_path = Path(handle.name)
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        count += len(chunk)
                        if count > MAX_PDF_BYTES:
                            raise ValueError(f"PDF exceeds {MAX_PDF_BYTES} byte limit: {final_url}")
                        if len(first_bytes) < 1024:
                            first_bytes.extend(chunk[: 1024 - len(first_bytes)])
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if b"%PDF-" not in first_bytes or count < 8:
                    raise ValueError(f"Response is not a PDF: {final_url}")
                checksum = digest.hexdigest()
                destination = PDF_DIR / f"{checksum}.pdf"
                if destination.exists() and sha256_file(destination) == checksum:
                    temporary_path.unlink()
                else:
                    os.replace(temporary_path, destination)
                temporary_path = None
                record_download(
                    conn, media_url, status="downloaded", final_url=final_url,
                    sha256=checksum, byte_count=count, content_type=content_type,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    raw_path=destination.relative_to(ROOT).as_posix(),
                )
                return "downloaded"
        except RetryableResponse as exc:
            if exc.status_code in (429, 503) and attempt == MAX_ATTEMPTS:
                raise CrawlHalt(
                    f"Repeated HTTP {exc.status_code} at {media_url}; stop and resume later"
                ) from exc
            if attempt < MAX_ATTEMPTS:
                print(f"  retry PDF HTTP {exc.status_code}: {media_url}", flush=True)
                retry_pause(attempt, exc.status_code, exc.retry_after)
                continue
            error = str(exc)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            if attempt < MAX_ATTEMPTS and (
                isinstance(exc, httpx.RequestError)
                or exc.response.status_code in RETRYABLE_STATUS
            ):
                print(f"  retry PDF transport/status error: {media_url}", flush=True)
                retry_pause(attempt)
                continue
            error = str(exc)
        except (OSError, ValueError) as exc:
            error = str(exc)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        record_download(conn, media_url, status="failed", error=error)
        print(f"  FAILED {media_url}: {error}", file=sys.stderr, flush=True)
        return "failed"
    raise AssertionError("unreachable")


def verify_archive(conn: duckdb.DuckDBPyConnection) -> int:
    rows = conn.execute(
        "SELECT media_url, raw_path, sha256, byte_count FROM downloads "
        "WHERE status = 'downloaded' ORDER BY media_url"
    ).fetchall()
    bad = []
    for media_url, raw_path, expected_hash, expected_size in rows:
        path = ROOT / raw_path
        if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
            bad.append(media_url)
    print(f"Verified {len(rows) - len(bad)}/{len(rows)} downloaded PDFs.")
    for media_url in bad:
        print(f"  missing or changed: {media_url}", file=sys.stderr)
    return len(bad)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--limit", type=int, default=DEFAULT_PDF_LIMIT,
        help=f"Maximum distinct PDFs from the snapshot (default: {DEFAULT_PDF_LIMIT})",
    )
    selection.add_argument(
        "--all", action="store_true", help="Explicitly fetch every PDF in the snapshot"
    )
    parser.add_argument(
        "--refresh-listings", action="store_true",
        help="Create a new listing snapshot instead of reusing/resuming the latest",
    )
    parser.add_argument(
        "--refetch-pdfs", action="store_true",
        help="Fetch selected PDF URLs again even when verified local bytes exist",
    )
    parser.add_argument(
        "--listing-only", action="store_true", help="Snapshot links without downloading PDFs"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="Check stored PDF sizes and hashes offline"
    )
    parser.add_argument(
        "--delay", type=float,
        help=("Minimum seconds between HTTP requests "
              f"(default: {REQUEST_DELAY_SECONDS} capped, "
              f"{BULK_REQUEST_DELAY_SECONDS} with --all)"),
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.delay is not None and args.delay < 0:
        parser.error("--delay cannot be negative")

    conn = connect_db()
    try:
        if args.verify_only:
            return 1 if verify_archive(conn) else 0

        snapshot_id, page_count = active_snapshot(conn, args.refresh_listings)
        delay = args.delay if args.delay is not None else (
            BULK_REQUEST_DELAY_SECONDS if args.all else REQUEST_DELAY_SECONDS
        )
        jitter = BULK_REQUEST_JITTER_SECONDS if args.all else 0.0
        limiter = RateLimiter(delay, jitter)
        print(f"Request pacing: {delay:.2f}s minimum"
              + (f" + up to {jitter:.2f}s jitter" if jitter else ""), flush=True)
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
        with httpx.Client(
            follow_redirects=True, timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            try:
                snapshot_listings(conn, client, limiter, snapshot_id, page_count)
            except CrawlHalt as exc:
                print(f"Crawl stopped during listing: {exc}", file=sys.stderr)
                return 2
            if args.listing_only:
                return 0
            limit = None if args.all else args.limit
            chosen = selected_pdfs(conn, snapshot_id, limit)
            print(f"Selected {len(chosen)} distinct PDFs "
                  f"({'all' if args.all else f'limit={limit}'}).", flush=True)
            counts = {"downloaded": 0, "cached": 0, "failed": 0}
            consecutive_failures = 0
            for ordinal, (media_url, title) in enumerate(chosen, start=1):
                try:
                    result = fetch_pdf(
                        conn, client, limiter, media_url, refetch=args.refetch_pdfs
                    )
                except CrawlHalt as exc:
                    record_download(conn, media_url, status="failed", error=str(exc))
                    print(f"Crawl stopped at {ordinal}/{len(chosen)}: {exc}",
                          file=sys.stderr, flush=True)
                    return 2
                counts[result] += 1
                print(f"  {ordinal}/{len(chosen)} {result}: {title}", flush=True)
                consecutive_failures = consecutive_failures + 1 if result == "failed" else 0
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"Crawl stopped after {consecutive_failures} consecutive PDF failures; "
                          "resume later after reviewing the errors.", file=sys.stderr)
                    return 2
            print("PDF result: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
            return 1 if counts["failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
