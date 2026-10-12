# Grouping quality beyond bracket captions

- Status: open
- Opened: 2026-10-12
- Needed: before the final run

**What:** About 200 cases have grouping flags or weak docket evidence, for
example a caption that disagrees with the court listing. About 118 documents
still find no docket in their caption after the bracket fix
([004](004-bracket-captions.md)) and fall back to the listing. The grouping
report (`python -m court report grouping september-cases`) lists them.

**Why it matters:** Grouping defines what a case is, so errors here carry
into every answer for that case.

**Recommendation:** Look for other general patterns, other caption layouts or
OCR damage, with automated checks against the court listing, as was done
for brackets. Make only rule changes that improve the data across the board;
no one-off fixes for individual cases. The pipeline must stay fully automatic.
