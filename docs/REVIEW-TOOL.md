# Local PDF text review

Run `make review`, then open `http://SERVER:8766/` on the server's
private-network address. The tool runs in the
project's Poppler-enabled Docker image and reads the PDF archive,
`data/text.duckdb`, and `data/triage_qa.duckdb` without writing to them. Stop
it with Ctrl-C. Set `REVIEW_PORT=8767` if the default host port is in use.

The compact toolbar leaves the rest of a desktop viewport for a single
PDF page and its text. The page fits within the left pane, and the right
pane scales text down to fit its available height. The right pane normally
starts with `current_analysis_pages.text` from the production `data/text.duckdb`
database. The **Native** button also reads production text. The **OCR**
button uses production OCR when available; otherwise it may show a comparison
from the separate QA sample, labeled `QA sample` in the text-pane tooltip.
QA data provides issue badges and flagged-page sampling, but never changes
the default selected text. Hover over the text pane for the source and
selection reason.

**Native OK pages** samples uniformly from the 18,253 pages currently marked
`native_ok`, including pages that the experimental screen flagged. **Flagged
pages** samples from the 804 `native_ok` pages with QA risk flags and the 556
pages triaged to a non-native path. **Unresolved** samples from the 23 pages
whose production text selection has status `unresolved`. On those pages the
viewer opens the available OCR text, or native text if OCR is empty, while
keeping the **Selected** tab available to show the reason no text was chosen.
The issue labels show why the sampled
page entered that mode. Previous and Next move within the current PDF, and
the URL contains the PDF checksum and page number so a finding can be
reopened. Neither clicking nor changing text sources records a judgment.

For example, the known zoning-map omission can be opened at:

<http://SERVER:8766/?sha=4c63de1d5d2e4d5704952bb9f56e7cefb51e0d81e7d473cc2e3693234152ba78&page=5>

The tool is intentionally read-only and limited to the private network. It is for fast visual
inspection, not a source of validated labels or an annotation system.
