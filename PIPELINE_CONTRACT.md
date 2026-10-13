# Output contract: one result per case

Every pipeline, whatever its steps, produces a results map: one result per
case under `appeal-result-2`, or the September pipeline's `appeal-result-1`
(`court/contracts/results.py`). Comparisons are
always `case ID → result`, across pipelines. Citations point into the case
text of the case set the results came from (`court/contracts/cases.py`). A
case the pipeline could not process is a failure item in the map, with its
kind and reason, not a result.

```jsonc
{
  "schema": "appeal-result-2",          // or appeal-result-1, which has no appeal_review
  "case": "g-225-10-05-vtec",
  "disposition": {                       // null only when processing failed before scope was known
    "value": "in_scope | excluded | uncertain",
    "reason": "text; required when excluded",
    "municipal_appeal": "yes | no | unknown",
    "housing": "yes | no | unknown",
    "evidence": [Evidence]
  },
  "records": [Record]                    // zero or more; an excluded case has none
}

Evidence = {"doc": 1, "page": 5, "quote": "exact continuous text"}
Date     = "YYYY-MM-DD" | "YYYY-MM" | "YYYY" | "unknown"

Record = {
  "docket": text | "unknown",
  "project": {"description": text | "unknown", "municipality": text | "unknown",
              "proposed_units": int | "unknown", "proposed_lots": int | "unknown", "evidence": [Evidence]},
  "municipal_decision": {"description": text | "unknown", "date": Date,
                         "outcome": "approved | approved_with_conditions | denied | other | unknown",
                         "evidence": [Evidence]},
  "appellants": [{"name": text | "unknown",
                  "relationship": "developer | neighbor | organization | other | unknown",
                  "seeks": "overturn_approval | overturn_denial | change_conditions | other | unknown",
                  "representation": "represented | pro_se | unknown", "evidence": [Evidence]}],   // empty = unknown
  "discretion": {"description": text | "unknown", "involved": "yes | no | unknown", "evidence": [Evidence],
                 "central": "yes | no | not_applicable | unknown", "central_evidence": [Evidence]},
  "observed_status": {"litigation": "ongoing | resolved | unknown",
                      "permission": "allowed | allowed_with_conditions | denied | unknown",
                      "description": text | "unknown", "evidence": [Evidence]},
  "timeline": [{"kind": "filing | judgment | further_appeal | reopening | remand | resolution",
                "date": Date, "court_or_body": text, "dockets": [text], "description": text,
                "evidence": [Evidence]}],
  "duration": {
    "start": {"date": "YYYY-MM-DD", "relation": "exact | on_or_before", "description": text,
              "evidence": [Evidence]} | "unknown",
    "end":   {"date": "YYYY-MM-DD", "relation": "exact | on_or_after", "description": text,
              "evidence": [Evidence]} | "unknown",
    "note": text,
    "kind": "exact | at_least | unknown",   // calculated from start and end
    "days": int | "unknown"                 // calculated from start and end
  },
  "restrictions": [{"description": text, "start": Date, "end": Date, "evidence": [Evidence]}],   // empty = none observed
  "housing_changes": [{"description": text, "evidence": [Evidence]}],                              // empty = none observed
  "related_dockets": [{"docket": text | "unknown", "relationship": text, "evidence": [Evidence]}], // empty = none observed
  "appeal_review": {"standard": "on_the_record | de_novo | unknown", "evidence": [Evidence]}       // appeal-result-2
}
```

- **Review standard** (`appeal-result-2`). Whether the court heard this
  docket's appeal on the record of the municipal proceeding (the town adopted
  on-the-record review, so the court reviews the board's record and findings)
  or de novo (the court hears the matter anew, taking its own evidence). It
  is `unknown` when the documents don't establish it, and it describes the
  appeal to the court, not later Supreme Court review.

- **Unknown.** Every field has one explicit `unknown`. Any field, including
  duration, may be unknown when a pipeline does not establish it.
- **Duration.** A pipeline supplies the evidenced start and end bounds; the day
  count is calculated (`results.measure`). Both endpoints exact gives an
  exact duration; otherwise a minimum (`at_least`). A zero-day minimum, a
  reversed interval or a missing endpoint establishes nothing (`unknown`).
- **Uncertain cases keep their records.** An uncertain disposition marks a case
  whose scope is not established; reducing uncertain dispositions is an
  experiment goal.
- **Evidence.** Citations are verified against the case text
  (`results.verify_quotes`); a step records the number checked and any not
  found on their page in the case's trace. An unverified quotation is a
  warning, not a failure.

`results.validate(result)` lists contract violations. When a run saves a
results map, a result that violates the contract becomes a failure.

## Failures and traces

A case the pipeline could not process is a failure item in the results map
instead of a result: `{"kind": "validation", "reason": "...", "step": "extract/review"}`.
Kinds include `validation` (a rejected answer), `delivery` (a failed or
uncertain model call), `not_sent` (a call the run did not send), `budget`,
`too_large` and `contract`. How each case moved through the steps (calls,
reused or new, warnings, rejections, quotation checks) is in the run's
`traces` dataset, not in the results.

## The September results

`september-results` is the September 2026 production run's outputs converted
to this contract once: scope categories became dispositions (confirmed → in
scope; a review's scope flag → uncertain with the review's reason), `unclear`
and `null` became `unknown`, and its 10 failed cases became failures. The
`production` pipeline reproduces its 1,385 results exactly from the saved
calls.

## Fact catalog and schema versions

`results.RECORD_FACTS` and `CASE_FACTS` name the research facts that
comparisons and the dashboard measure, each with how to read its value and
evidence from a result. Each fact records the schema version that introduced
it. Adding a field means a new schema version that declares it (with its
allowed values and `unknown`) and a fact for it; results made under the older
version remain valid, and comparisons show the fact as *newly measured* rather
than more specific.
