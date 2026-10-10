# Output contract: one result per case

Every pipeline, whatever its steps, produces one result per case under
`appeal-result-1` (`appeal_schema.py`). Comparisons are always `case ID →
result`, across pipelines. The input boundary is the frozen, page-addressable
source text of each case (`pipeline/corpus.py`); text extraction and OCR are
not part of a pipeline.

```jsonc
{
  "schema": "appeal-result-1",
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
  "related_dockets": [{"docket": text | "unknown", "relationship": text, "evidence": [Evidence]}] // empty = none observed
}
```

- **Unknown.** Every field has one explicit `unknown`. Any field, including
  duration, may be unknown when a pipeline does not establish it.
- **Duration.** A pipeline supplies the evidenced start and end bounds; the day
  count is calculated (`appeal_schema.measure`). Both endpoints exact gives an
  exact duration; otherwise a minimum (`at_least`). A zero-day minimum, a
  reversed interval or a missing endpoint establishes nothing (`unknown`).
- **Uncertain cases keep their records.** An uncertain disposition marks a case
  whose scope is not established; reducing uncertain dispositions is an
  experiment goal.
- **Evidence.** Citations are verified against the frozen source text; the
  number checked and the paths of any not found on their page are recorded in
  the case's processing status. An unverified quotation is a warning, not a
  failure.

`appeal_schema.validate(result)` lists contract violations. A result that
violates the contract fails its case.

## Processing status

Status is run metadata stored beside each result, not research output:

```json
{"state": "complete | failed | in_progress",
 "errors": [{"step": "finishing/review", "error": "...", "type": "validation", "retryable": true}],
 "warnings": ["..."],
 "quotes": {"checked": 12, "unverified": ["records[0].timeline[2].evidence[0]"]}}
```

A failed case keeps the disposition it had reached and has no records. Its
draft records remain in the run's diagnostics only.

## Run directories

```text
data/runs/NAME/                    standalone runs (data/experiments/NAME/run for experiments)
  run.json                         pipeline ID, corpus, cases with source hashes, samples, sealed
  ledger.jsonl                     calls made or reused, by content key
  results/<case>.json              appeal-result-1
  status/<case>.json               processing status
  traces/<case>.json               how the case moved through the steps
  executions.json                  each execution's options, spending and outcome
```

`pipeline.runs.load(folder, case_ids)` returns the run record and its cases
with status, disposition, records and source reference. Evaluators, cohorts and
the dashboard read runs only through it.

## Baselines

- `data/runs/production` is the production run rebuilt by the ported pipeline
  from its saved calls, with sending disabled. `acceptance.json` records that it
  matches `data/runs/production-converted`, production's own outputs converted
  to this contract, case for case.
- `appeal_runner.py import-legacy` converts any other legacy `appeals-1` run.
  Converted runs have no traces. In production's conversion, scope categories
  became dispositions (confirmed → in scope; a scope flag → uncertain with the
  review's reason), `unclear` and `null` became `unknown`, and failed groups
  became failed cases without records.

## Fact catalog and schema versions

`appeal_schema.RECORD_FACTS` and `CASE_FACTS` name the research facts that
comparisons and the dashboard measure, each with how to read its value and
evidence from a result. Each fact records the schema version that introduced
it. Adding a field means a new schema version that declares it (with its
allowed values and `unknown`) and a fact for it; results made under the older
version remain valid, and comparisons show the fact as *newly measured* rather
than more specific.
