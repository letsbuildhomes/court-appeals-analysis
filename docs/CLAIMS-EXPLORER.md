# Claims explorer

Run `make claims` and open <http://127.0.0.1:8767/>. It uses the existing
Docker image with Poppler, serves only on localhost, and opens the archive,
text, and scouting DuckDB databases read-only. No API key is needed. Stop it
with Ctrl-C.

The global **Schema** selector switches between V1, V2, and V3. The front page
defaults to PDFs extracted with that version; choose **All PDFs** to see the
full text inventory. Titles and posted dates come from the archive listing.
Counts and failure status refer only to the selected version. Each detail page
uses that version's fields and has an **Attempt** selector when a document has
several successful attempts in that version. The URL preserves schema,
document, and chosen attempt for sharing or reloads.

V3 detail pages show a project summary, compact entity cards, and a card for
each decision-making process. A court process contains the appealed decision,
timeline, and court questions with answers and reasons. Each evidence page is
a compact source button; quotes and raw JSON stay collapsed until opened.
The count index jumps to the main sections. **Show source** renders the cited
PDF page beside the claims using Poppler. The source pane can move between
pages, and **Open full PDF** opens the original archived file. Validation
warnings and model uncertainties are shown separately. Model strings are
rendered as text, not executable HTML.

The existing successful sample used the frozen `scout_v0.md` prompt and appears
under logical V1. Any `scout_v1.md` results also appear under V1, with their
actual prompt filename shown in the attempt selector. V2 uses the new
`scout_v2.md` prompt and remains unchanged. V3 uses the separate
`scout_v3.md` prompt and its process-centered layout. The explorer displays
extracted claims; it does not validate legal conclusions or link projects
across documents.

The **Group runs** page at `/runs` shows two-turn docket-group batches: token
use by step (including cached input), each group's triage route and
extraction, per-PDF membership basis, every turn's raw response and warnings,
and source pages for `{doc, page}` citations. This is the retained legacy viewer;
the supported municipal housing extraction and dashboard instructions are in
[APPEALS.md](APPEALS.md).
