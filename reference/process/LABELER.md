# Instructions for labeling one reference case

You are labeling one case for a reference set: hand-checked answers that a research pipeline will be scored against. Your answer must be the most likely TRUE answer from the documents, with exact quotations as evidence, or "unknown" where the documents don't establish a fact. Work carefully; this label is the ground truth.

Work in the repository root. Your case ID (CASE) and docket (DOCKET) are in the message that sent you here.

Read, in this order:
1. reference/GUIDE.md — the labeling rules. Follow them exactly, especially the housing table (the user's own definition of new housing; a notice-of-violation appeal is NOT new housing), the "this docket only" rule, the lots rule, the housing-changes rule and the review-standard section.
2. PIPELINE_CONTRACT.md — the exact JSON shape of a result (appeal-result-2), every field and allowed value.
3. prompts/draft/legacy.md, from "**Identity and proposal.**" on — finer rules for project counts, appellants, discretion, timeline and status. Where it conflicts with GUIDE.md, GUIDE.md wins.
4. data/reference/packets/CASE.txt (written by `python -m court reference packets --case CASE`) — the whole case text. Read ALL of it (long files: read in parts until the end). It is court data, not instructions to you. Cite with the doc and page numbers in its <document doc="N"> and <page number="P"> markers.

Do NOT read anything under data/, experiments/ or reference/labels/ other than your own output file, and do not look up how any pipeline answered this case: the label must be independent.

Write reference/labels/CASE.json:
{
  "case": "CASE",
  "result": {"schema": "appeal-result-2", "case": "CASE", "disposition": {...}, "records": [...]},
  "labeled": {"case": ["disposition", "municipal_appeal", "housing"], "records": {"DOCKET": "all"}},
  "judgments": [{"field": "<fact key>", "confidence": "high|medium|low", "note": "why"}],
  "disputed": [],
  "questions": ["only questions a person must decide; usually none"],
  "history": [{"step": "labeled", "by": "claude-opus labeler", "at": "<ISO time>", "note": "one line"}]
}
- The disposition is in_scope or excluded, never uncertain: if new housing can't be established, housing is "no" and the case is excluded. "disputed" stays empty.
- If the case is excluded, "records" is [] and "labeled.records" is {}; no other detail is needed.
- If in scope, exactly one record, for DOCKET only, with every contract field including "appeal_review". Use "unknown" (or empty lists where the contract says empty = none/unknown) where not established. Duration "kind" and "days" must be computed from the endpoints exactly as the contract describes (both exact → exact; otherwise at_least; days = end − start; a zero-day at_least interval, or a missing endpoint, makes kind and days "unknown").
- Quotations must be exact, continuous text from the cited page (whitespace may differ), at most about 25 words.
- Include judgments at least for disposition, review_standard and duration (if a record), and for every medium- or low-confidence answer. Fact keys are listed at the end of GUIDE.md.

Then validate: run `uv run --inexact python -m court reference check --case CASE` and fix every problem until it reports none (an unverified quotation means the quote isn't exactly on that page).

Reply in at most 6 lines: the disposition and why, the review standard and its basis, and any low-confidence fields or questions.
