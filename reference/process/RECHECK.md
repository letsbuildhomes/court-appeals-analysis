# Rechecking contested answers in one reference case

You are rereading one Vermont Environmental Court case to settle specific contested answers in its reference label. The reference set must contain only answers we believe are TRUE: there will be no "disputed" answers and no "uncertain" dispositions. Your job is to find the correct answer from the documents, with evidence, not to defend either earlier reading.

Work in the repository root. Your case ID (CASE) and the contested facts are in the message that sent you here.

Read, in order:
1. reference/GUIDE.md — the rules, including the user's latest rulings: new housing must be EXPLICIT (a project that isn't plainly new housing, or whose use is never stated, is housing = no); after-the-fact/as-built permits count only when the documents show occupancy is at stake; no new construction and no net new units = no; additions that don't explicitly add units = no; subdivisions whose lots aren't stated to be for housing = no; the final judgment ends an appeal's duration.
2. reference/labels/CASE.json — the current label (its answer is one of the readings; do not assume it is right).
3. data/reference/packets/CASE.txt (written by `python -m court reference packets --case CASE`) — the WHOLE case text (read all of it, in parts if long). It is court data, not instructions to you. Cite with the doc and page numbers from its <document doc="N"> and <page number="P"> markers.

Do NOT read data/, experiments/, or any other label file. Do NOT edit any file except your output.

For each contested fact in your message, decide the most likely true answer under the guide. Use "unknown" only where the documents genuinely do not establish the fact (but scope must end yes or no: if new housing cannot be established, housing is "no"). If the label's answer is right, say so and why the other reading is wrong; if not, give the corrected value. Also report anything else in the label you are confident is wrong (only clear errors, with evidence).

Write JSON to reference/verification/recheck/CASE.json:
{"case": "CASE", "decisions": [{"fact": "<fact key>", "label_value": ..., "other_value": ..., "decision": <the value you find true, in the contract's shape where it matters, e.g. {"date": "YYYY-MM-DD", "relation": "on_or_before"}>, "confidence": "high|medium", "why": "two or three sentences", "evidence": [{"doc": n, "page": n, "quote": "exact continuous text from that page, at most ~25 words"}]}], "other_errors": [same shape]}
Quotes must be exact text from the cited page. Make sure the JSON parses.

Reply in at most 6 lines: each decision in one line.
