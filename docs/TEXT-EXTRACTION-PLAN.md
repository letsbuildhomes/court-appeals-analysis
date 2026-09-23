# Proposed text extraction and OCR stage

Status: design only, September 23, 2026. No archive-wide text extraction or OCR has run.

## Evidence from a small local probe

The archive has 2,560 unique PDFs. Poppler's `pdftotext` read all pages in two read-only samples: 50 files spaced through listing order and 100 files selected with a fixed random seed. In the random sample, **7 of 100 PDFs had zero extractable text**; spot checks showed page-sized scanned images. Four more had at least one sparse page despite substantial text elsewhere. In the spaced sample, one otherwise readable PDF had an empty final page with no embedded image, consistent with a blank page. These samples establish that OCR is needed and that empty text alone does not prove OCR is needed; they are not a census or a quality estimate for the whole archive.

The installed tools are `pdfinfo`, `pdftotext`, `pdftoppm`, and `pdfimages` from Poppler. Tesseract and OCRmyPDF are not currently installed. One listing URL points to a `.docm` file, which belongs in a separate non-PDF inventory status rather than the OCR queue.

## Proposed pipeline

1. **Inventory each unique PDF hash once.** Read `downloads` where `status='downloaded'`, group by SHA-256, and retain every source URL through the existing table. Record page count, parser version, parse errors, and a file checksum check. Never alter the archived PDF bytes.
2. **Extract native text by page.** Use `pdftotext` with UTF-8 and stable page separators; verify that the number of page records matches `pdfinfo`. Store text and page number with the source PDF hash, extraction method, tool version, character and word counts, and output status. Keep the unnormalized text as evidence; downstream cleanup can be versioned separately.
3. **Triage sparse pages.** Flag pages with near-zero or implausible text. For each flagged page, inspect image presence/coverage or render it to distinguish a scan from a truly blank page. Flag suspicious text layers (e.g. mostly broken characters) for review. The first thresholds should be calibrated against a manually checked pilot, not treated as proof of readability.
4. **OCR only pages that need it.** Render selected pages at approximately 300 DPI with `pdftoppm`, then run local Tesseract in English. Keep its plain text and word-level TSV/confidence output, along with page number, image resolution, engine version, and any error. OCR may run on a page with existing but unusable native text; preserve both versions.
5. **Choose analysis text with provenance.** A page-level view selects native text, OCR text, or blank/error status. It must retain both underlying results so reviewers can see how a quoted passage was obtained. Do not silently replace native text with OCR or infer missing words.
6. **Review the pilot before scaling.** Include scan-only documents, mixed documents, blank pages, and normal born-digital PDFs. Compare rendered pages against extracted text, check page counts and ordering, and inspect low-confidence OCR. Run the full stage only after the pilot distinguishes scans from blanks reliably.

## Proposed data contract

Use DuckDB tables keyed by `(pdf_sha256, page_number, method, tool_version)` for extraction results, with separate document/page inventory and triage status. `method` is `native` or `ocr`; each result keeps the text, status, character count, and provenance. Record attempts and errors so reruns process only missing or outdated work. A derived page-text view chooses the preferred result for later classification while preserving the original results. The document key is the **PDF hash**, not a listing URL or court case number; project/case linking happens later.

## Tool choice

Poppler already works locally for native extraction and rendering. For this project's analytical goal, **page-level Tesseract** is the first OCR choice because it can target only flagged pages and return word-level TSV with confidence. OCRmyPDF is useful if searchable derivative PDFs become a deliverable, but its `skip` mode skips pages that have existing text, including pages whose text layer is poor; its `redo` mode changes the text layer. Keep the original archive regardless. See [Tesseract's CLI documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html) and [OCRmyPDF processing modes](https://ocrmypdf.readthedocs.io/en/stable/advanced.html).

## Implementation boundary

Start with a capped, resumable text-inventory command and an OCR pilot. Do not OCR all 2,560 files by default. Completion means every archived PDF and every page has an explicit status: usable native text, OCR result, confirmed blank, or needs review/error. The one `.docm` link is reported outside that PDF denominator.
