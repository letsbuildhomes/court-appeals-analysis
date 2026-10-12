# Native-text triage QA experiment

> Historical record. The code this describes has been replaced and its commands no longer run; see [ARCHITECTURE.md](ARCHITECTURE.md) for the current system.

This is validation and tuning work, not a production text-selection stage. The
script reads the local PDF archive and `data/text.duckdb` and writes only
`data/triage_qa.duckdb`. It does not change native text, triage, selected text,
or document audit results. The QA database can be deleted and regenerated.

## Run or resume

```sh
make text-qa-screen       # Metadata, word boxes, and 512-pixel ink maps
make text-qa-compare      # OCR comparison on a fixed 1,000-page sample
make text-qa-recompare    # Reapply comparison rules to saved OCR TSV; no OCR work
make text-qa-status       # Checkpoints and category counts
```

`make text-qa` runs both stages. For a small screen pilot, use
`make text-qa-screen QA_ARGS='--limit 3'`. A completed PDF is skipped on the
next screen run; a completed sampled page is skipped on the next comparison
run. All commands use the existing Docker image with Poppler and Tesseract,
with no network access. An individual PDF or OCR failure is recorded and the
run proceeds; the command exits unsuccessfully so failures remain visible.

The QA version includes the native and triage versions, OCR tool version, and
ink-rendering scale. Changing one creates a fresh set of checkpoints.

## Screen on every PDF

For each PDF, `pdfimages -list` supplies image dimensions and effective
resolution, and `pdftotext -bbox` supplies native word positions. A 512-pixel
grayscale render supplies overall and five-band dark-pixel shares on each
`native_ok` page. Existing native text supplies character counts and the
document-relative median. The screen records these signals separately from
the rule-based flags:

| Flag | Trigger | Why it matters |
| --- | --- | --- |
| `page_sized_image` | Largest image covers at least 50% of page area | A visible raster may contain words outside the native layer. |
| `substantial_image` | Largest image covers at least 20% | Similar concern at lower coverage. |
| `body_ink_without_native_words` | Middle three page bands have at least 1% ink, but fewer than five native words in the body | Possible unrepresented visible content. |
| `poppler_output_disagreement` | At least 20 positioned words, but under 90% of their tokens occur in existing native text | Extractor-output consistency check. Both outputs use Poppler, so agreement cannot establish correctness. |
| `short_native_text` | Fewer than 500 letters or digits | May be a genuine short page or incomplete layer. |
| `sparse_relative_to_document` | Under 25% of document median letters or digits and at least 1% body ink | Outlier within the same PDF. |
| `suspicious_characters` | Existing native extraction reports bad characters | Encoding warning. |

Flags make a page `risk_flagged`; they do not replace its production triage
decision. All screen measurements and flags are in `qa_pages`; per-PDF
checkpoints are in `screen_documents`.

## Fixed OCR comparison

After every PDF is screened, the script chooses 500 `native_ok` pages by a
stable SHA-256 sort and up to 500 additional pages with the highest screen
risk scores. This gives one broad sample and one enriched sample; the enriched
group must not be treated as representative of all pages. Membership is in
`qa_sample`.

The existing 2,200-pixel Poppler/Tesseract OCR function renders each sampled
page and produces a TSV with word confidence and position. The comparison
normalizes words to case-folded alphanumeric tokens of at least three
characters. It counts confident OCR words (confidence at least 80) missing
from the native text as a multiset, plus the longest consecutive missing
sequence and missing words in the middle 60% of the page. Results and the
raw OCR text/TSV are in `qa_ocr`.

| Comparison category | Rule |
| --- | --- |
| `ocr_inconclusive` | Fewer than 10 confident OCR words. |
| `ocr_agrees` | At most 10% of confident OCR words are absent and no missing sequence reaches five words. |
| `suspected_native_omission` | At least 20 confident words, at least 20% missing, a missing run of eight words, and either eight missing body words or an image/ink risk signal. |
| `ocr_disagreement` | Other comparison disagreement. |
| `qa_error` | Rendering, OCR, or parsing failure. |

These are **signals, not a measured transcription error rate**. OCR can be
wrong, especially for unusual layouts, stamps, handwriting, and legal
formatting. Both native extraction and the word-box check use Poppler, so
their agreement is not independent verification. The fixed random sample can
estimate how often this *detector* fires; it cannot measure false negatives
without trusted text. The current experiment preserves every page's
measurements so thresholds can be re-evaluated before production changes.

## Results

Run completed September 24, 2026. The screen created 18,809 page rows for all
2,560 PDFs. All 18,253 `native_ok` pages have screen measurements; 556 pages
were already on the OCR/blank paths. There were no unresolved QA processing
errors. Seven PDFs initially emitted XML-invalid control characters from
Poppler's word-box output; the parser was fixed and a checkpointed retry
completed those PDFs.

| Screen result | Pages |
| --- | ---: |
| `native_ok`, no screen signal | 17,449 |
| `native_ok`, at least one risk flag | 804 |
| Existing non-native triage path | 556 |

Flags overlap: 702 `short_native_text`, 374
`sparse_relative_to_document`, 55 `page_sized_image`, 2
`substantial_image`, and 6 `suspicious_characters`. The word-box
consistency and body-ink-without-words rules fired on no pages. The former
shares the Poppler extractor with native text, so this absence adds little
independent reassurance.

| OCR result after corrected TSV parsing | Random 500 | Risk-enriched 500 |
| --- | ---: | ---: |
| `ocr_agrees` | 499 | 484 |
| `ocr_disagreement` | 0 | 3 |
| `ocr_inconclusive` | 1 | 12 |
| `suspected_native_omission` | 0 | 1 |
| Processing error | 0 | 0 |

The one strong omission signal is a zoning-map page: native text contains
the surrounding court prose but not several readable map-legend labels
found by OCR. It had `substantial_image`, `short_native_text`, and
`sparse_relative_to_document` flags. The three other disagreements are in
the risk sample; one is another map page, one has a page-sized image, and
one is a short footnote page with OCR word splitting. The random sample has
no disagreement signal under these rules, but that is not a guarantee that
all other native pages are complete. In particular, OCR could miss the same
content as native extraction.

### Parser defect found during validation

Tesseract TSV text cells may contain unescaped quote characters. Python's
default CSV quote handling can then absorb subsequent TSV rows into one
cell, creating spurious OCR text and false missing-word detections. The
initial QA comparison reported six strong omission signals. A regression
test exposed the problem; the experiment now splits each TSV line at the
first 11 tabs and recomputed all 1,000 comparisons from saved TSV without
rerunning OCR. The corrected count is **one**.

The production `text_finish.py` OCR reader had the same defect.
Reconstructing its 534 original OCR outputs from saved TSV changed 17 text
rows, of which 16 had been selected as analysis text. It was corrected in
the next pipeline version: all 534 pages were reparsed from saved TSV, and
selection and audit were refreshed. The current production OCR text now
matches the corrected parser on all 534 pages. The QA experiment itself
remains in its separate database.

### Processing cost and interpretation

The full screen took about five minutes on this machine. OCR on the 1,000-page
sample took about 21 minutes; reclassification from saved TSV took about
three seconds. These are observed wall-clock times, not projected cloud
costs. The random sample was selected by a fixed hash, and the risk sample
was selected by score. The risk sample contains many genuinely short pages,
so its 12 inconclusive OCR results are expected; it cannot be pooled with
the random group to estimate prevalence. The 983 agreement results indicate
that the two methods usually recover similar text under this comparator,
not that either is a verified transcription.
