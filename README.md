# Vermont Environmental Division PDF archive

This project begins with the Vermont Judiciary's [Environmental opinions listing](https://www.vtcourts.gov/opinions-decisions?f%5B0%5D=court_division_opinions_library_%3A3). The listing supplies PDF links and audit metadata. The court PDFs are the only research data input. See [proposal.html](proposal.html) for the full pipeline design and limits of inference, and [docs/INGEST.md](docs/INGEST.md) for the implemented extractor, current archive status, and next handoff.

## Run the first ingest step

Install [uv](https://docs.astral.sh/uv/) if needed, then run:

```sh
UV_CACHE_DIR=.uv-cache uv sync --locked
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

Verify archived files offline with `make verify`. Raw and generated directories are excluded from Git. The page inventory, native-text extraction, and triage are in [text_pipeline.py](text_pipeline.py). OCR, analysis-text selection, and automated audit are in [text_finish.py](text_finish.py). See [docs/TEXT-PIPELINE.md](docs/TEXT-PIPELINE.md) for the stages, commands, output tables, and limits.

## Current checkpoint

The September 22, 2026 snapshot contains 104 listing pages, 2,581 posting rows, and 2,578 distinct media URLs. As of September 23, 2,577 URLs have successful download records, representing 2,560 distinct PDF files. The remaining URL redirects to a `.docm` file and was rejected by the PDF-only downloader. See [docs/INGEST.md](docs/INGEST.md) for the exact status and operating details.

The local text pass inventoried and extracted **18,809 pages** from those 2,560 PDFs. Triage marked 18,253 pages as having usable native text, 502 as needing OCR, 32 as ambiguous OCR candidates, and 22 as provisionally blank. All 534 OCR candidates were processed. A separate QA experiment compared 1,000 `native_ok` pages against OCR, and the read-only page viewer supports visual inspection; neither makes `native_ok` a transcription guarantee.

The Docker/Poppler/Tesseract rerun produced a current OCR result for all **534** queued pages. Selection uses native text on **18,253** pages and OCR text on **511**; **23** pages remain unresolved and **22** retain the provisional blank status. The audit covers all **2,560** PDFs: **2,535** are operationally ready under current triage and **25** are uncertain. A repeat full run skipped all previously processed PDFs and pages. These are extraction outcomes, not a validation of every native-text page; see [docs/TEXT-PIPELINE.md](docs/TEXT-PIPELINE.md).

## Run the text stages

The text stages run in Docker. The image installs Poppler and Tesseract, and uses the checked-in `uv.lock` for Python packages. The host does not need those PDF/OCR executables. `make` checks the Docker build cache before each text command; the downloaded PDFs and archive database are mounted as inputs, and only `data/text.duckdb` is written.

```sh
make text-complete  # Resume inventory, native, triage, OCR, selection, and audit
make text-status    # Inspect page totals, text choices, and audit flags
make text-check     # Read-only production coverage gate
```

The production database is `data/text.duckdb`; downstream code reads its `current_analysis_pages` view. Completed PDFs are skipped on rerun; details and capped commands are in [docs/TEXT-PIPELINE.md](docs/TEXT-PIPELINE.md).

## Experimental native-text QA

`make text-qa` screens the archived PDFs for native-text risk signals, then
compares a fixed sample of 1,000 `native_ok` pages with Tesseract OCR. It is a
validation experiment and writes only `data/triage_qa.duckdb`; it does not
change the production text choice. See
[docs/TRIAGE-QA-EXPERIMENT.md](docs/TRIAGE-QA-EXPERIMENT.md) for rules, commands,
and results.

For a quick visual check, run `make review` and open
<http://127.0.0.1:8766/>. The read-only split-screen tool shows one PDF page
beside its production-selected, native, or available OCR text, with random sampling of
`native_ok` or flagged pages. See [docs/REVIEW-TOOL.md](docs/REVIEW-TOOL.md).
The [image inventory](docs/IMAGE-INVENTORY.md) counts embedded raster images
and records why visual-exhibit descriptions are deferred.

The proposed next phase—scouting document claims, linking cases and projects,
then rolling out in small batches—is outlined in
[docs/DOCUMENT-ANALYSIS-PLAN.md](docs/DOCUMENT-ANALYSIS-PLAN.md).
