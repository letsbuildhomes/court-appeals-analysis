# Phase 0–1 ingest: operator guide and handoff

This document describes **implemented behavior** as of September 23, 2026. The [proposal](../proposal.html) covers the intended later pipeline. The court PDFs are the only research data input; the court listing is used to discover those PDFs and preserve provenance.

## Current checkpoint

The latest completed snapshot is `20260922T204351Z-76e9f5ca` from the [Vermont Judiciary Environmental opinions listing](https://www.vtcourts.gov/opinions-decisions?f%5B0%5D=court_division_opinions_library_%3A3). It has **104 pages**, **2,581 posting rows**, and **2,578 distinct media URLs**. These are archive postings, not a count of housing projects, municipal appeals, or cases.

The initial capped runs proved cache reuse and verified the first ten URL records. A subsequent bulk run populated **2,577 successful URL records**, representing **2,560 unique PDF files** (about 457 MiB). `make verify` passed for all **2,577/2,577** successful URL records on September 23. One URL, `/media/3427`, points to a `.docm` document rather than a PDF; the PDF-only downloader correctly rejected it and recorded `status='failed'`. A repeat full-archive run will retry that URL until it is explicitly classified as a non-PDF source. No persisted text extraction pipeline, OCR, housing filtering, case linking, or timeline analysis has run.

## Setup and commands

Requires Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/). From the project root:

```sh
UV_CACHE_DIR=.uv-cache uv sync
make extract             # Snapshot the listing; select at most five distinct URLs
make extract LIMIT=10    # Reuse verified cache; select the first ten URLs
make verify              # Offline size and SHA-256 check of downloaded records
```

Other explicit modes:

```sh
uv run python extract.py --listing-only
make refresh-listings LIMIT=10
uv run python extract.py --limit 10 --refetch-pdfs
make extract-all
```

`--listing-only` records the listing without selecting PDFs. `--refresh-listings` starts a new listing snapshot; the old snapshot remains in the database and on disk. `--refetch-pdfs` fetches selected URLs again even if local bytes verify. `make extract-all` removes the safety cap. `--delay SECONDS` overrides the minimum spacing between HTTP request starts. The source and storage paths are defined in [`config.py`](../config.py); [`Makefile`](../Makefile) wraps the CLI. [`pyproject.toml`](../pyproject.toml) and `uv.lock` pin the Python environment.

## Data flow and storage contract

1. [`extract.py`](../extract.py) opens or creates `data/archive.duckdb`. By default it reuses the most recent listing snapshot, including an incomplete one that needs resuming. `--refresh-listings` creates a new snapshot.
2. It reads the Environmental opinions listing and its numbered pages in order. Parsed posting fields and the original HTML are stored under one snapshot ID. A locally saved page is reused only when its recorded SHA-256 matches the file; a missing or changed page is fetched again.
3. It selects distinct `media_url` values in first-seen listing order, up to the requested cap. This cap counts **URLs**, not unique byte hashes, cases, or housing projects.
4. For each selected URL, a prior successful record is used without HTTP only when the file exists and its size and SHA-256 still match. Otherwise the downloader fetches the URL, follows the court redirect, streams bytes into a temporary file, checks for a PDF header and a 100 MiB size ceiling, computes SHA-256, and atomically stores `in/pdfs/<sha256>.pdf`. Different URLs with identical bytes share that file.
5. Each URL gets a `downloads` audit row with success or failure. A rerun retries failed or invalid cached URLs. A run returns a nonzero exit code if any selected URL fails. Network requests are sequential, with up to four attempts for transport errors, HTTP 429, and selected 5xx statuses. PDF redirects are followed manually so each HTTP request is paced.

### Bulk-download policy

`make extract-all` reuses the saved listing snapshot and verified PDF URLs. For uncached URLs it spaces **each request**, including `/media/` redirects, by at least **1.5 seconds plus 0–0.5 seconds of random jitter**. Capped runs retain a 0.5-second default.

On HTTP 429 the downloader waits at least 60, then 120, then 240 seconds before retries, respecting a longer `Retry-After` header. Retryable 5xx responses use 15, 30, and 60 seconds. It stops the entire run after repeated 429s or 503s, on HTTP 403, if the server requests a delay longer than 15 minutes, or after three consecutive PDF failures. Other transient failures are retried and recorded. If the run stops, rerun `make extract-all` later: verified PDFs are skipped before HTTP and failed URLs are retried. The court site's [robots.txt](https://www.vtcourts.gov/robots.txt), checked September 22, 2026, published no crawl delay and did not disallow the listing or PDF paths; this pacing is a conservative project setting, not a site-specified rate.

The output paths are relative to the project root:

| Path | Contents |
| --- | --- |
| `in/listings/<snapshot_id>/page_XXXX.html` | Saved source HTML (`XXXX` is a zero-based page index) |
| `in/pdfs/<sha256>.pdf` | PDF content addressed by SHA-256 |
| `data/archive.duckdb` | Discovery and download metadata |

`in/`, `data/`, `out/`, `.venv/`, and `.uv-cache/` are ignored by Git. Preserve `in/` and `data/archive.duckdb` together to keep the archive and its provenance usable. Listing metadata is an audit/discovery aid; title, date, docket, and division in these rows are not yet normalized or validated against PDF text.

## DuckDB tables

| Table | Grain / key | Important fields |
| --- | --- | --- |
| `snapshots` | One crawl; `snapshot_id` | `source_url`, `started_at`, `completed_at`, `page_count`, `status` |
| `listing_pages` | One page per snapshot; (`snapshot_id`, `page_index`) | `page_url`, `fetched_at`, HTML `sha256`, `raw_path`, `entry_count` |
| `listing_entries` | One posting per page; (`snapshot_id`, `page_index`, `ordinal`) | `media_url`, `title`, `displayed_date`, `date_attribute`, `division`, `docket_raw` |
| `downloads` | One current result per media URL; `media_url` | `final_url`, `downloaded_at`, PDF `sha256`, `byte_count`, `content_type`, `etag`, `last_modified`, `raw_path`, `status`, `error` |

`downloads.status` is `downloaded` or `failed`; the table does not keep a history of every attempt. `downloaded_at` is the time the current row was recorded, including a failure row. A cache hit does not update the row. Repeated listing posts keep separate `listing_entries` rows. Media URLs are deduplicated only for download selection. Dates stay as the site displayed them, including missing or malformed values.

Example read-only inspection:

```sh
uv run python - <<'PY'
import duckdb
db = duckdb.connect('data/archive.duckdb', read_only=True)
print(db.sql('SELECT snapshot_id, page_count, status FROM snapshots ORDER BY started_at DESC').fetchall())
print(db.sql("SELECT status, count(*) FROM downloads GROUP BY status").fetchall())
print(db.sql("SELECT sha256, count(*) AS urls FROM downloads WHERE status = 'downloaded' GROUP BY sha256 HAVING count(*) > 1").fetchall())
PY
```

## Recovery and interpretation

- Rerun the same capped command after an interruption. The latest snapshot resumes missing or corrupt pages, and valid PDF URL records are skipped before any PDF request. `make verify` checks stored PDF size and hash without contacting the court site.
- A new listing snapshot requires `--refresh-listings`. The default cached snapshot does not detect new or removed postings. A cached PDF URL is likewise not checked for a changed remote file unless `--refetch-pdfs` is used.
- Each listing page is fetched separately, so a snapshot is **not** an atomic point-in-time export of the court site. Page counts or ordering could shift during a long crawl. A source layout change causes parsing to fail rather than silently accepting zero rows.
- The downloader checks the PDF signature near the start and local integrity. The full archive passed a size and SHA-256 check, but renderability, page count, text quality, relevance, and completeness of the site's archive have not been validated at scale. A 150-document read-only text probe is described in [TEXT-EXTRACTION-PLAN.md](TEXT-EXTRACTION-PLAN.md).
- The listing includes both municipal and Act 250 matters; consolidated cases can contain both. Nothing here classifies appeal type or grounds. Later stages must distinguish documents, dockets, appeals, and housing projects, with evidence from PDF pages and explicit uncertainty.

## Next implementation step

Build a **document inventory/text extraction stage** over the 2,560 unique content-addressed PDFs. The proposed page-level workflow and OCR pilot are in [TEXT-EXTRACTION-PLAN.md](TEXT-EXTRACTION-PLAN.md). The later relevance, case-linking, chronology, and appeal-ground stages remain design work in the proposal; they have not been implemented or validated.
