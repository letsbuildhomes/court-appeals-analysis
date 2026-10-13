# Reference set

About 60 cases with hand-checked answers, which experiments are scored
against (`lab new --reference`). The code is `court/reference/workspace.py`;
the commands are `python -m court reference
freeze|pools|packets|check|approve|export`.

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

The instructions each reader follows are in `process/`: `LABELER.md`,
`VERIFIER.md` and `RECHECK.md`. Readers work from each case's text,
written to `data/reference/packets/` by `reference packets`.

1. **Label.** A strong model reads the whole case and writes
   `labels/CASE.json` following `GUIDE.md` and `process/LABELER.md`, without
   seeing any pipeline's answers.
2. **Check.** `reference check` validates the result against the contract,
   that records are for the case's own docket, the labeled-fact lists, and
   every quotation against the frozen text.
3. **Verify.** A second, independent reader answers the low-confidence and key
   facts (scope, outcome, dates, units, review standard); a quick reader checks
   the rest.
4. **Adjudicate.** Every disagreement is settled by rereading the source
   (`process/RECHECK.md`; the rereads are kept in `verification/recheck/`).
   A finished label has no disputed facts and no uncertain scope, and
   `reference check` rejects either.
5. **Approve.** A person reviews in the web app's Reference section (each
   case's answers beside the text they cite); `reference approve` marks the
   verified labels approved, and `reference export` writes them as datasets.

Statuses in `manifest.json`: chosen → labeled → verified → approved.

## Exporting

```sh
python -m court reference export                                   # every approved case, named "reference"
python -m court reference export --split development --name reference
python -m court reference export --split holdout --name reference
```

The split exports are named `reference-development` and `reference-holdout`.
An experiment records the dataset ID it was created with, so exporting again
moves the names to the new dataset without changing earlier experiments.

## Adding cases

To add, say, ten more in-scope cases:

1. Add a target to `profile.json`, such as `{"split": "development",
   "layer": "targeted", "category": "more-in-scope", "count": 10}`, and raise
   `total`.
2. Choose candidates from `pools/` (for in-scope cases, `random-in-scope` and
   `high-unknown`), skipping any already in `manifest.json`. Read each before
   choosing: under the guide's definition many cases the legacy run called in
   scope are excluded. Held-back and development cases may not share a
   document; `reference check` enforces it.
3. Add a row per case to `manifest.json` with status `chosen` (copy an
   existing row's shape: split, layer, category, reason, hints).
4. `python -m court reference packets --case A,B,C` writes their texts.
5. Label, check, verify and adjudicate as above, moving each status along.
6. Review them in the web app, then `reference approve --case A,B,C` and
   export again.

## Source

`source.json` names the frozen case set (one docket per case). Every label's
citations point into its text; changing the grouping means relabeling.
