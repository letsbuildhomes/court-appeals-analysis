# PDF image inventory and deferred visual-content work

The September 24, 2026 QA screen ran Poppler `pdfimages -list` on every one of
the 2,560 archived PDFs. Its page-level results are in `data/triage_qa.duckdb`
(`qa_pages.image_count` and `qa_pages.max_image_area`). `make text-qa-status`
reprints the counts below. These are **raster-image occurrences**, not unique
images or semantic exhibits. The script ignores mask rows, and vector-drawn
figures are not included.

| Inventory measure | Count |
| --- | ---: |
| Raster-image occurrences | 2,036 |
| Pages containing at least one raster image | 1,876 |
| PDFs containing at least one raster image | 1,041 |
| Image-bearing `native_ok` pages | 1,372 |
| Image-bearing `ocr_needed` pages | 501 |
| Image-bearing `ocr_candidate` pages | 3 |

On 554 pages, the largest single raster covers at least half of the page.
On four pages it covers 20–50%; on 1,318 pages it covers under 20%. The
remaining 16,933 pages have no raster-image row. These area estimates use
pixel dimensions and effective display resolution from Poppler; multiple
small images can collectively cover more of a page than the largest-image
figure suggests. One PDF page may also contain dozens of image pieces.

## Current decision

Visual review found that image-bearing pages are predominantly document scans
whose text is handled by the existing OCR path. Raster-image counts alone
cannot identify semantic figures. The QA sample did find a zoning map whose
legend had readable labels omitted by native extraction, so the corpus is not
free of meaningful visual content. The project will proceed without a general
image-description stage. Maps, plans, and other visual exhibits are an
explicit coverage limit of the production text output, not silently appended
to court prose. The raw PDFs and image inventory remain available if later
appeal analysis shows that figure content matters.

## Feasibility if revisited

The PDFs alone are enough to build a visual-content stage. Poppler can
extract raster objects with `pdfimages -all`, and it can render a full page
or cropped area for figures that are vector-drawn or split into image pieces.
A vision model could then produce a description and visible-text transcript
for selected maps, plans, charts, or photos. The known zoning-map omission
shows why page context matters: OCR recovered legend labels that native text
omitted, while the figure is assembled from several image objects.

The 2,036 occurrences should **not** become 2,036 model calls by default.
A sensible later experiment would:

1. Preserve each image occurrence's PDF checksum, page, object identity,
   dimensions, and page placement; hash extracted bytes to find repeated
   logos or other duplicates.
2. Distinguish likely document scans and small decorative images from
   substantive figures using size, location, native-text coverage, and a
   sampled visual check. Use a page rendering when an image object lacks
   enough context or the figure is vector-drawn.
3. Send selected page images or crops to a vision model, asking separately
   for visible text and a factual description. Store both with source-page
   coordinates, model/prompt version, and uncertainty. Do not silently
   splice generated descriptions into the court's original prose.
4. Compare the vision output against native text and Tesseract OCR on a
   bounded sample before expanding it across the archive.

This is a feasible extension, but its model workload and useful yield remain
unknown until the image types are sampled and deduplicated. No vision-model
processing was run for this inventory, and none is required for the current
text-stage handoff.
