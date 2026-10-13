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

## Decision (2026-10-13)

**A case is one docket.** A document belongs to every docket its page-1
heading names, so a decision issued in several dockets is shared by their
cases; nothing is merged, and the body text is never read for grouping.
Related appeals (decided together, a remand appealed again under a new
number, the same project's Act 250 and municipal appeals) become links, not
merged cases. Grouping appeals into projects may come later as its own layer.

Why: merging chained unrelated appeals through body citations (Ghia with
Doyle and Lussier & Noe; Stanak with Daniels and Gulli). More often it
blended related but distinct appeals, with different appellants, outcomes
and filing dates, into one case. Research facts are per docket, so cases now
match them.

Evidence: agents read about 60 decisions in full, in four random samples.
- 12 groups the change splits: 0 were unrelated joins, but all 12 blended 2–9
  distinct appeals; splitting was better in all 12 and lost essential context
  in none.
- 16 decisions naming several dockets: 9 decide distinct appeals the text
  separates; 6 are one ruling for all (a motion, a contempt order), which
  becomes the same answer in each case.
- 16 decisions mentioning "formerly", remand or consolidation: no appeal was
  renumbered; Supreme Court remands keep their number. One appeal rarely
  carries two numbers: a project that superseded an earlier one, an appeal
  of a reconsideration, a remand to the town board appealed again, typos.
- 22 decisions where heading and listing disagree: the heading was right in
  all 22; the listing was blank, wrong or incomplete in 16 (Supreme Court
  numbers, typos, consolidated dockets left out).

## Progress

- 2026-10-13: The grouping step is `cases/group:docket-cases`, tested by the
  experiment `docket-cases` with extraction variants that tell a shared case
  which docket is its own (`triage:docket-target`, `draft:docket-target`,
  `review:docket-target`). Offline, over all 2,560 documents: 1,731 cases
  (1,395 before); 1,141 have exactly the text they had, so their saved model
  answers are reused; 476 share a document with another docket's case. The
  heading reader fixes dockets written with Unicode dashes (79 documents
  read no docket before), stacked dockets in a caption column, Supreme Court
  orders' "appealed from" docket, footnote markers and OCR's letter O. It no
  longer reads body mentions (188 documents). 69 documents name no docket in
  their heading and use the listing; 61 listing dockets that no heading names
  are flagged rather than made into cases, and Supreme Court numbers
  (`eo05-120`, `22-AP-00296`) never become cases. Only 2 of September's 513
  records have a docket that no longer has a case (one a Superior Court
  number).
- 2026-10-13: Agents checked the result. Of 34 documents whose reading changed
  or was flagged, 31 are assigned correctly; 2 miss a docket (a number split
  across lines, a second docket on an unlabeled "Project:" row) and 1 was a
  Supreme Court "AP" number made into a case, since fixed. Of 20 sampled
  cases, 13 are good, 7 usable with caveats (interlocutory orders only,
  enforcement actions with no appellant, multi-docket orders that decide only
  some of their dockets) and none wrong.
- 2026-10-13: The probe picks 682 cases whose input changed, plus 39 removed.
  Expected cost from September's averages: about $7 off-peak for the probe and
  $9 more for the confirm round, double at peak hours. The dry-run plan's
  worst-case reservations are much higher ($64 for the probe at peak).
