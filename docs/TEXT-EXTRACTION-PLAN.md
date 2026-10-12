# Text extraction and OCR plan

> Historical record. The code this describes has been replaced and its commands no longer run; see [ARCHITECTURE.md](ARCHITECTURE.md) for the current system.

Status: historical design record. Inventory, native extraction, triage, OCR, page-text selection, and automated audit are implemented; see [TEXT-PIPELINE.md](TEXT-PIPELINE.md). A separate 1,000-page `native_ok` QA comparison is documented in [TRIAGE-QA-EXPERIMENT.md](TRIAGE-QA-EXPERIMENT.md).

## Evidence from a small local probe

The archive has 2,560 unique PDFs. Poppler's `pdftotext` read all pages in two read-only samples: 50 files spaced through listing order and 100 files selected with a fixed random seed. In the random sample, **7 of 100 PDFs had zero extractable text**; spot checks showed page-sized scanned images. Four more had at least one sparse page despite substantial text elsewhere. In the spaced sample, one otherwise readable PDF had an empty final page with no embedded image, consistent with a blank page. These samples establish that OCR is needed and that empty text alone does not prove OCR is needed; they are not a census or a quality estimate for the whole archive.

The text stages run in Docker with Poppler's `pdfinfo`, `pdftotext`, `pdftoppm`, and `pdfimages` and Tesseract OCR. Python packages are installed from `uv.lock` inside the image. One listing URL points to a `.docm` file, which remains a source issue outside the PDF page denominator.

## Proposed pipeline

1. **Inventory each unique PDF hash once.** Read `downloads` where `status='downloaded'`, group by SHA-256, and retain every source URL through the existing table. Record page count, parser version, parse errors, and a file checksum check. Never alter the archived PDF bytes.
2. **Extract native text by page.** Use `pdftotext` with UTF-8 and stable page separators; verify that the number of page records matches `pdfinfo`. Store text and page number with the source PDF hash, extraction method, tool version, character and word counts, and output status. Keep the unnormalized text as evidence; downstream cleanup can be versioned separately.
3. **Triage sparse pages.** Flag pages with near-zero or implausible text. For each flagged page, inspect image presence/coverage or render it to distinguish a scan from a truly blank page. Suspicious native text and other ambiguous pages become automated OCR candidates. The current thresholds are not proof of native-text completeness; the later QA comparison tested them on a sample.
4. **OCR only pages that need it.** Render selected pages with Poppler, then run Tesseract. Keep its plain text and word-level TSV geometry and confidence, along with page number, render size, engine version, and any error. OCR may run on a page with existing but unusable native text; preserve both versions.
5. **Choose analysis text with provenance.** A page-level view selects native text, OCR text, or blank/error status. It must retain both underlying results so reviewers can see how a quoted passage was obtained. Do not silently replace native text with OCR or infer missing words.
6. **Audit outputs.** Check page counts, ordering, source links, selection coverage, and OCR quality flags automatically. Keep unresolved pages explicit rather than making a manual review queue a required pipeline step. A separate QA experiment used OCR comparisons to test the current `native_ok` triage rule.

## Proposed data contract

Use DuckDB tables keyed by `(pdf_sha256, page_number, method, tool_version)` for extraction results, with separate document/page inventory and triage status. `method` is `native` or `ocr`; each result keeps the text, status, character count, and provenance. Record attempts and errors so reruns process only missing or outdated work. A derived page-text view chooses the preferred result for later classification while preserving the original results. The document key is the **PDF hash**, not a listing URL or court case number; project/case linking happens later.

## Tool choice

The implementation uses **page-level Tesseract OCR** on Poppler-rendered images. The image includes English training data; word-level TSV output is stored with confidence and geometry for later audit. OCRmyPDF would only be relevant if searchable derivative PDFs became a deliverable. Keep the original archive regardless.

## Implementation boundary

The current inventory/native/triage pass covers all 2,560 PDFs and 18,809 pages. Triage queues 502 zero-text pages and 32 ambiguous pages for OCR, while 22 pages are provisionally blank. The OCR/selection/audit stages are resumable and page-level; they do not OCR all 2,560 files. Every archived PDF page receives an explicit selection outcome: native text, OCR text, blank, or unresolved. The one `.docm` link is reported outside the PDF denominator. The 1,000-page QA comparison is complete; blank-page verification and semantic interpretation of visual exhibits remain outside the current text pipeline.
