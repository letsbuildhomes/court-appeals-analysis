# Reference set

About 60 cases with hand-checked answers, which experiments are scored
against (`lab new --reference`). The code is `court/reference/workspace.py`;
the commands are `python -m court reference freeze|pools|check|export`.

## How the cases are chosen

`profile.json` sets the counts. Two layers:

- **Random** (30): drawn at random within the legacy run's answer (excluded, in
  scope, uncertain), so results can be reweighted to the whole corpus and the
  set holds ordinary cases. Candidates are taken in their pool's random order;
  one is skipped only when unusable (unreadable, no text), and the skip is
  noted.
- **Targeted** (30): cases with many unknowns in the legacy run, exclusions that
  turned on housing, decisions shared by several dockets, enforcement actions,
  cases with only interim orders, the legacy run's failures and very long cases.

12 are held back (`split: holdout`) and used only to confirm a change before
adopting it. Held-back and development cases share no document. Across the
set, each era and grouping shape has a minimum (`spread`). The slate is
approved by a person before labeling.

Report the layers separately: the random layer estimates corpus accuracy, the
targeted layer finds failure modes.

## How a case is labeled

1. **Label.** A strong model reads the whole case and writes
   `labels/CASE.json` following `GUIDE.md`, without seeing any pipeline's
   answers.
2. **Check.** `reference check` validates the result against the contract,
   that records are for the case's own docket, the labeled-fact lists, and
   every quotation against the frozen text.
3. **Verify.** A second, independent reader answers the low-confidence and key
   facts (scope, outcome, dates, units, review standard); a quick reader checks
   the rest.
4. **Adjudicate.** Disagreements are settled by rereading the source, or left
   `disputed`, which removes them from scoring.
5. **Approve.** A person reviews; `reference export` writes approved labels.

Statuses in `manifest.json`: chosen → labeled → verified → approved.

## Source

`source.json` names the frozen case set (one docket per case). Every label's
citations point into its text; changing the grouping means relabeling.
