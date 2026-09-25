# PDF text pipeline

Implemented September 23–24, 2026. This local, resumable pipeline reads `data/archive.duckdb` and `in/pdfs/` without modifying either. All production text, decisions, and checkpoints go to `data/text.duckdb`. The court PDFs are the only research-data input. All text stages run in a network-disabled Docker container with Poppler and Tesseract; Python dependencies are installed from `uv.lock` inside the image.

## Run it

Docker is the only host requirement for the text stages. `make text-*` builds the image when its Dockerfile or dependency lock changes, then runs the stage inside the container. If the image has been removed, the target rebuilds it. The repository and PDFs are mounted read-only, `data/text.duckdb` is writable through the `data/` mount, and `data/archive.duckdb` has an additional read-only mount. From the project root:

The Dockerfile pins its Python and uv base images by digest. Debian's Poppler and Tesseract packages are installed from the package repository at image-build time, so a clean build in the future could pick up a newer package release. Tool versions are recorded in DuckDB stage keys; if they change, the pipeline recomputes affected output instead of silently reusing it. Keep the built image if byte-for-byte environment recreation is required.

```sh
make text-complete  # all six stages; resumes by default
make text-status    # current counts, source choices, and audit flags
make text-check     # read-only coverage and source-contract checks; exits nonzero on failure
make text-test      # offline tests inside the same container
```

The first three stages can run separately with `make text-inventory`, `make text-native`, and `make text-triage`. `make text-all` runs those first three stages. `make text-ocr`, `make text-select`, and `make text-audit` run the later stages, and `make text-finish` runs those three together. For a small run or one PDF, pass arguments through `TEXT_ARGS`:

```sh
make text-complete TEXT_ARGS='--limit 10'
make text-complete TEXT_ARGS='--sha <full-64-character-pdf-sha256>'
```

`--limit` counts newly processed PDFs in **each** stage; already complete PDFs are skipped and do not consume the limit. OCR processes every queued page in each selected PDF. Repeating a capped run advances to unfinished work. The CLI stores stage runs and failure counts in `stage_runs`; it returns a nonzero exit code when a stage records a processing error. An OCR failure is recorded per page, does not stop other pages, and is retried on the next run. An interrupted run keeps its committed page results and is marked `interrupted` when the next finishing stage starts. Source download problems remain in the archive DB and are mirrored in `source_issues`.

## Current outputs

| Item | Count |
| --- | ---: |
| Unique PDFs inventoried and readable | 2,560 |
| Pages inventoried and natively extracted | 18,809 |
| Pages with usable native text | 18,253 |
| Pages queued for OCR | 534: 502 without native text and 32 ambiguous |
| Pages classified blank | 22 |
| Current OCR results | 534 complete; 0 engine failures |
| Pages selected from OCR | 511 |
| Pages unresolved after OCR | 23 |
| Audited PDFs | 2,535 ready; 25 uncertain; 0 incomplete |

The 2,577 successful URL records refer to 2,560 unique PDF byte hashes; each hash is processed once. One listing URL resolves to a `.docm` file, not a PDF; it is represented as a source issue and does not enter the PDF page denominator. An offline archive integrity check passed for all 2,577 successful URL records before the text pass. The Docker rerun completed all six stages with Poppler 22.12.0 and Tesseract 5.3.0. The Python environment came from the checked-in `uv.lock`.

The read-only `make text-check` gate passes on the current database: inventory, native, triage, selection, and analysis views each have the same 18,809 page keys; all 534 queued pages have completed OCR; all 2,560 PDFs have current audit rows; and every usable analysis page has nonempty text and a valid source. It deliberately allows explicit `blank` and `unresolved` outcomes and `uncertain` document audits. A future extraction change should pass this gate before downstream analysis uses the new output.

Selection gives all 18,809 inventoried pages an outcome: 18,253 native text, 511 OCR text, 22 provisional blanks, and 23 unresolved. Among unresolved pages, 13 have empty Tesseract text, eight have fewer than ten letters/digits, and two have mean OCR word confidence below the selection threshold of 45. All 23 are flagged in the audit. Three selected OCR pages separately have mean word confidence below 60 and receive audit flags. No page is missing a selection row. A repeat `make text-complete` skipped all 2,560 PDFs in inventory/native/triage, all 534 OCR pages, and all 2,560 PDFs in selection/audit.

## Tables and checkpoints

| Table or view | Grain and role |
| --- | --- |
| `documents` | One row per PDF SHA-256; path, byte count, modification time, page count, inventory status/error and version |
| `document_pages` | One row per inventoried `(PDF hash, 1-based page number)` |
| `native_documents` | One completion or failure record per PDF and extractor version |
| `native_pages` | Original Poppler text and basic quality counts per `(PDF hash, page, extractor version)` |
| `page_triage` | One decision and reason per `(PDF hash, page, triage version)`; rendered ink measure and image indicators when needed |
| `current_native_pages`, `current_page_triage` | Views that select the active versions for analysis without counting old versions twice |
| `ocr_queue` | Current `ocr_needed` and `ocr_candidate` pages, with PDF path and page number |
| `ocr_candidate_queue` | Ambiguous or suspicious subset of the OCR queue |
| `ocr_pages`, `current_ocr_pages` | Tesseract text, original word TSV, line/word detail JSON, quality counts, engine/render version, result ID, and errors per page |
| `selection_documents`, `selected_pages` | Resumable document checkpoints and page decisions keyed to native/triage/OCR versions |
| `current_analysis_pages` | One page-ordered chosen text or explicit blank/unresolved outcome, with provenance |
| `audit_documents`, `audit_page_flags` | Document readiness and automated page flags |
| `source_issues` | Failed/non-PDF URL records mirrored from the archive ledger |
| `stage_runs`, `current_versions` | Run summaries and active rule/tool versions |

Inventory checks local file size and SHA-256, then asks `pdfinfo` for page count. Native extraction runs `pdftotext -enc UTF-8 -layout` once per PDF, splits page breaks, and requires the extracted page count to match the inventory. Triage reads those page rows. Each document's database writes are transactional, so an interruption repeats at most the unfinished document in the active stage. A complete row set is skipped on rerun. A changed source file fails the source check rather than silently reusing text. Rule and Poppler versions are part of completion keys so future revised extraction/triage can coexist with prior results.

## Triage rules and limits

The current rules mark substantial native text (`>=80` letters/digits) as `native_ok`. A short page (`>=20`) without a large embedded image also keeps native text. Sparse pages are rendered at 512 pixels on the long edge; zero-text pages with almost no dark pixels are `blank`, while visible zero-text pages are `ocr_needed`. Short ambiguous text and suspicious replacement/control characters become `ocr_candidate`. Both OCR labels feed the same automated queue. No human or agent review is needed to run the pipeline. These rules measure extractability, not guaranteed completeness; the separate [QA experiment](TRIAGE-QA-EXPERIMENT.md) tested 1,000 `native_ok` pages without changing the production decision.

`make text-status` prints the current queue size and the number of ambiguous OCR candidates. The `ocr_queue` and `ocr_candidate_queue` DuckDB views retain page-level reasons for read-only queries.

## OCR, selection, and audit

`text_finish.py` renders only queued pages with Poppler's `pdftoppm`, capping the long edge at 2,200 pixels, and runs Tesseract with English language data and automatic page segmentation (`--psm 3`). It stores plain text, the original word-level TSV, line and word details as JSON, mean word confidence, and low-confidence share. Rendered images are temporary; the engine/render versions and settings are recorded. A PDF source checksum/size/mtime check precedes OCR. A completed `(PDF hash, page, OCR version)` is skipped on rerun, while failures are retried. The current parser splits raw TSV at the first 11 tabs because Tesseract does not escape quotes in its text field. Version 2 reparsed all 534 cached version-1 TSV results without rerendering pages, then selection and audit were refreshed; future OCR pages use the corrected parser directly.

Selection never overwrites native or OCR output. `native_ok` uses native text; `blank` remains blank. OCR candidates use OCR when native text is absent/garbled or OCR recovers substantially more readable content. When OCR and native text agree, the readable native text remains selected. Missing, poor, or conflicting results become `unresolved`. Selection stores a pointer to the chosen result, rather than duplicating text, and recomputes a document if an OCR result changes. These are conservative rules, not a claim that OCR confidence is a calibrated probability.

Audit checks that inventoried pages have one selected outcome, that selected text exists, and that OCR dependencies and confidence are recorded. It writes page flags and document statuses `ready`, `uncertain`, or `incomplete`. `ready` means operationally complete under the current triage rules, not transcription-certified. `uncertain` is a completed audit outcome, not a request for manual review. The separate [QA experiment](TRIAGE-QA-EXPERIMENT.md) found one strong native-text omission signal on a map page among 1,000 sampled `native_ok` pages; the 22 provisional blank pages have not received the same independent check.

## Production handoff and remaining limits

The page viewer's default **Selected** tab reads `current_analysis_pages.text` directly from `data/text.duckdb`. Its native text and production OCR tabs also read that database. The optional QA database supplies risk labels and, where production OCR does not exist, an OCR comparison for the viewer's **OCR** tab only. Neither QA results nor viewer activity change the selected text. See [REVIEW-TOOL.md](REVIEW-TOOL.md).

Downstream code should query `current_analysis_pages` in `(pdf_sha256, page_number)` order and retain `status`, `source`, `reason`, and version columns. For example:

```sql
SELECT pdf_sha256, page_number, status, source, reason, text
FROM current_analysis_pages
ORDER BY pdf_sha256, page_number;
```

The grain is a PDF byte hash and page number. A hash can correspond to multiple listing URLs; a court case or housing project can span multiple PDFs. Linking those entities belongs to later stages. Do not treat the 23 `unresolved` pages as empty text or silently omit them from denominators. The 22 blank pages are provisional. Text on maps, plans, and other visual exhibits is not systematically described; [the image inventory](IMAGE-INVENTORY.md) records this gap. The QA comparison and visual spot checks support the current extraction strategy but do not establish a transcription error rate.
