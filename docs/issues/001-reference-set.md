# Build the reference set

- Status: open
- Opened: 2026-10-12
- Needed: before experiments

**What:** About 50 cases with checked, correct answers, saved in the same
results format the pipeline produces. The lab compares an experiment's answers
with these to tell whether a change made answers *right*, not just *different*.

**Why it matters:** Without it, the lab can only report that answers changed
or became more specific. A more specific answer can still be wrong, so most
extraction experiments can't be judged.

**Options and pieces:**
- *Selection.* Choose cases on purpose, not at random: in-scope and uncertain
  cases, several multi-docket cases, long texts, the 10 cases that failed in
  September, some with grouping flags, and some excluded cases as a control.
- *Labeling.* A strong model works through each case with the full text and
  drafts answers in the results format; a person checks them. Fields nobody
  checked are marked "not labeled" and aren't scored (the comparison already
  supports this). The tool that drafts and records labels doesn't exist yet.
- *Holdout.* Tuning against the same 50 answers again and again starts to fit
  those answers. Keep about 10 back and use them only for adoption checks.
- *Grouping changes.* If a grouping change splits or merges a reference case,
  that case needs relabeling; the case-set diff shows when.

**Recommendation:** Fix the bracket caption grouping ([004](004-bracket-captions.md))
first so reference cases don't need relabeling. Then draft a candidate list
with reasons for approval before spending anything on labeling.
`docs/reference-selection-proposal.md` has a more formal sampling proposal.

## Progress

- 2026-10-13: Plan agreed with the user. The reference set is drawn from the
  one-docket case list ([015](015-grouping-quality.md)). It mixes a random
  slice of all cases, so ordinary exclusions are in it and triage can be
  scored, with deliberately chosen hard cases. Selection uses September's
  saved answers and case shapes as hints, without triaging the corpus again;
  the labels establish the true scope. The baseline is the new pipeline run
  over the reference set alone (`lab baseline`, about $1), and experiments
  such as a Jev triage are compared against it. The user wants to discuss the
  selection criteria before a slate is drafted.
- 2026-10-13: The workspace is `reference/` (`court/reference/workspace.py`,
  `python -m court reference freeze|pools|check|export`), with a labeling
  guide that includes the new review-standard question. The case set is
  frozen (`reference/source.json`). Haiku readers summarized 175 pooled
  candidates; the slate of 60 was chosen by reading them (`SLATE.md`):
  27 in scope, 23 excluded, 8 uncertain and 2 failed by September's answers;
  every era and grouping shape represented; no document shared across the
  development and held-back splits. It awaits the user's approval.
- 2026-10-13: Five development cases labeled by Opus, checked, verified blind
  by Sonnet and adjudicated. The user's rulings are in `GUIDE.md`: new housing
  means building or majorly renovating a structure that serves as housing
  (including duplex conversions, accessory apartments, seasonal-to-year-round
  conversions, house lots, care homes, and replacements of a new structure);
  violation-notice appeals are out of scope; lots are 0 only when the
  documents make clear none are created; housing changes are changes to the
  housing itself; facts never cross dockets; excluded cases carry no detail.
  No new questions: the court's effect and who prevailed follow from the
  board's decision and the final permission, and can be derived in code for
  the dashboard and snapshot.
- 2026-10-13: All 60 cases labeled by Opus (one labeler per case, blind to
  every pipeline's answers), checked, verified blind by Sonnet (scope for
  excluded cases, key facts for the rest) and adjudicated. Scope agreed in 44
  of 50 verified cases; the rest were settled by rereading or kept with a
  judgment note. Rulings added to the guide: one appellant entry per named
  person, "et al." leaves the count unknown, and representation by a
  non-lawyer is unknown. Open for the user: whether an access approval whose
  only purpose is new house lots counts as new housing; whether post-judgment
  proceedings years later extend an appeal's duration; whether a faculty
  residence at an institute is housing. Then the user's review and approval.
