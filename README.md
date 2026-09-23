# Vermont Environmental Division PDF archive

This project begins with the Vermont Judiciary's [Environmental opinions listing](https://www.vtcourts.gov/opinions-decisions?f%5B0%5D=court_division_opinions_library_%3A3). The listing supplies PDF links and audit metadata. The court PDFs are the only research data input. See [proposal.html](proposal.html) for the full pipeline design and limits of inference, and [docs/INGEST.md](docs/INGEST.md) for the implemented extractor, current archive status, and next handoff.

## Run the first ingest step

Install [uv](https://docs.astral.sh/uv/) if needed, then run:

```sh
UV_CACHE_DIR=.uv-cache uv sync
make extract
```

`make extract` snapshots **all listing pages and links**, then downloads the **first five distinct PDF URLs** in listing order. It does not filter for housing yet. The first run takes longer because the source has many paginated listing pages and requests are spaced apart. Subsequent runs reuse the latest snapshot and locally verified PDF bytes without requesting cached URLs again.

To choose another small cap, use `make extract LIMIT=10`. An uncapped run requires `make extract-all`; it uses slower, jittered pacing and stops on repeated rate limiting. `--refresh-listings` creates a fresh snapshot; `--refetch-pdfs` re-downloads selected URLs. `--listing-only` fetches links without PDFs.

## Outputs

| Path | Purpose |
| --- | --- |
| `in/listings/<snapshot_id>/page_XXXX.html` | Saved HTML pages from one listing snapshot |
| `in/pdfs/<sha256>.pdf` | PDF bytes named by SHA-256 content hash |
| `data/archive.duckdb` | Snapshot, posting, and download audit tables |

The DuckDB tables are `snapshots`, `listing_pages`, `listing_entries`, and `downloads`. Each listing row keeps the site's title, displayed date, raw date attribute, division, docket text, PDF URL, page number, and order. Missing or malformed source dates remain uncorrected. A successful download records its original media URL, redirect destination, content type, byte count, ETag, Last-Modified header, SHA-256, and local path. A failed download is recorded with an error.

The downloader writes to a temporary `.part` file, checks for PDF bytes, enforces a 100 MiB size ceiling, computes SHA-256 while streaming, and atomically moves the file into the archive. It verifies cached files before skipping them. Exact duplicate PDF bytes share one content-addressed file while their listing records remain separate.

Verify archived files offline with `make verify`. Raw and generated directories are excluded from Git. The next pipeline phase will read these PDFs for native text extraction and selective OCR; see [docs/TEXT-EXTRACTION-PLAN.md](docs/TEXT-EXTRACTION-PLAN.md).

## Current checkpoint

The September 22, 2026 snapshot contains 104 listing pages, 2,581 posting rows, and 2,578 distinct media URLs. As of September 23, 2,577 URLs have successful download records, representing 2,560 distinct PDF files. The remaining URL redirects to a `.docm` file and was rejected by the PDF-only downloader. See [docs/INGEST.md](docs/INGEST.md) for the exact status and operating details.
