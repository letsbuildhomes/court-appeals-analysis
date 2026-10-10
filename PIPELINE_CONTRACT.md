# Frozen-text pipeline boundary

`appeal_contracts.adapt_run(run_dir)` reads an existing `appeals-1` run under
its shared lock and returns an `appeal-cases-1` JSON value. It does not run
OCR, select cases, contact a model, or modify the run. Source text and frozen
prompt hashes are verified by default. The adapter rejects concurrent writers
and inputs that change during the read.

The contract separates research results from the steps used to produce them:

```python
from appeal_contracts import adapt_run

artifact = adapt_run(run_dir)
for case in artifact['cases']:
    status = case['execution']['status']
    category = case['scope_category']  # confirmed / uncertain / excluded
    for record in case['records']:    # finalized docket records only
        print(case['case_id'], record['docket'], record['duration'])
```

Every manifest group has a case, including excluded, failed, unfinished, and
uncertain groups. `case_id` and `group_id` are the same run-local identity;
cross-run comparisons must also check `source.sha256`. A docket may be null.
Do not join different groups merely because their docket strings match.

Each case contains `source` (frozen text path/hash, documents and source docket
identities), `execution` (effective status and checkpoint diagnostics), `scope`,
`scope_category`, `route`, and `records`. Only `records` is finalized output.
`candidate`, `review`, and `duration_bounds` preserve implementation diagnostics
and drafts, which must not be counted as finalized facts. Final records remove
draft annotations and include finalized duration and its evidence bounds.
Scope classification and processing completion are separate: complete uncertain
cases stay uncertain. Failed cases retain their drafts but have no final records.

The top-level `recipe` describes components, edges, configuration, and prompt
identities. It records the current implementation; its shape does not require
future implementations to use the same component topology. Frozen prompt text
is retained for inspection and comparison. Component `prompt_sha256` hashes
its direct prompt file; `prompt_dependencies` lists shared and inherited prompt
hashes, and `effective_prompt_sha256` fingerprints that dependency map. This is
a prompt-content fingerprint, not a complete request hash: source text, model
responses, and instructions embedded in runner code also affect actual calls.
Unknown individual hashes are null rather than copied from the aggregate hash.
`provenance` records input hashes,
verification, and inherited recorded costs. Unknown historical costs remain
unknown; inherited charges are not new experiment spending. Historical code
revision must not be inferred from the current checkout.

# Experiment artifacts

```sh
python appeal_experiments.py import-run /path/to/existing/run /path/to/new/experiment
python appeal_experiments.py inspect /path/to/new/experiment
```

This creates a new directory containing `manifest.json` (`appeal-experiment-1`)
and `cases.json` (`appeal-cases-1`). Existing outputs are never overwritten and
output inside the source run is rejected. `read_experiment(folder)` validates
the artifact digest, version, count, and unique case identities and returns
`{'manifest': ..., 'canonical': ..., 'cases': [...]}`. Evaluators and dashboards
can use this read API without understanding pipeline checkpoint layout.

The manifest identifies this as a `historical_baseline`, records zero new model
calls/cost, and leaves `reference` null. `--code-revision` is the adapter's code
revision, not evidence of the historical run's code revision. The canonical
file preserves the recipe, inputs, drafts, final results, and provenance;
evaluations and reference labels should be separate artifacts.

The current runner remains `appeals.py`; this entry point imports saved output
only. The [experiment runner](docs/EXPERIMENT-RUNNER.md) selects cohorts, runs
targeted variants through `appeals.py`, and publishes `experiment_candidate`
artifacts with cohort-restricted baselines (`create_experiment(case_ids=...)`).
Alternate strategies and reference generation are not implemented. Future producers
can emit the versioned canonical contract and matching experiment manifest
without reproducing the historical checkpoint topology. Source paths in a
canonical artifact are relative to its source run; the artifact stores hashes
and results, not copies of the frozen source text.

These artifacts may contain source prose and evidence and are internal research
artifacts. The public research MCP continues using its existing restricted
SQLite projection and does not expose this canonical envelope.
