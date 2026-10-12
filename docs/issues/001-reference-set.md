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
