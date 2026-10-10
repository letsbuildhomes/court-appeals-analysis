# Experiment runner

An experiment reruns a frozen cohort from a base run with a set of overrides
and compares the result with the base run. Its definition is a directory of
files in `experiments/NAME/`, kept in git; its run data goes to
`data/experiments/NAME/`, which is not. See [the pipeline](PIPELINE.md) for
recipes, steps and providers.

```text
experiments/NAME/ ──plan──▶ data/experiments/NAME/run   (no model calls)
                             run ──▶ paid calls under a cap on new spending
                             finalize ──▶ baseline/ candidate/ comparison.json
```

## 1. Create a definition

```sh
make experiments-init EXPERIMENT=review-tokens EXPERIMENT_ARGS='\
  --base-run data/production-import-UazDoN/data/appeals/full-run \
  --hypothesis "A larger review output allowance resolves output-limit failures"'
make experiments-cohort COHORT_ARGS='experiments/review-tokens/cohort.json \
  --from data/experiments/production-baseline-relocated \
  --name review-failures --description "All saved review failures" --status review_failed'
```

`init` writes `experiment.json` and a `README.md` to fill in. A cohort
(`appeal-cohort-1`) saves its query and the case IDs and source hashes it
selected. Query fields combine with AND; repeated values are alternatives:
`--case`, `--status`, `--scope`, `--duration-kind`. `--sample N --seed S`
takes a reproducible sample. Existing cohort files are never replaced.

### Definition files

| File | Purpose |
| --- | --- |
| `experiment.json` | `hypothesis`, `base_run` (relative to the repository), `cohort` file, and options below |
| `cohort.json` | The frozen case selection |
| `recipe.json` | Optional fragment deep-merged into the base run's recipe; `null` removes a key; a `prompts` entry maps a prompt name to a file in this directory |
| `prompts/NAME.md` | Optional replacement or new prompt, by name |
| `components/*.py` | Optional new step components |
| `README.md` | Why, what changes, and how to judge it |

`experiment.json` options: `companions` (default `true`) keeps each cohort
case's base review-batch companions; `rerun` lists steps to call afresh even
when an identical saved call exists, which is how a same-cohort variation
control is run; `allow_batch_change` accepts a batch membership change the
cohort would otherwise cause.

Examples of `recipe.json` fragments:

```json
{"steps": {"review": {"settings": {"max_output_tokens": 131072}},
           "duration": {"settings": {"max_output_tokens": 131072}}}}
```

```json
{"phases": {"finishing": {"batch": {"kind": "single"}}},
 "steps": {"extract": {"context": "fresh"}}}
```

```json
{"providers": {"strong": {"model": "another-model", "env": "STRONG"}},
 "steps": {"review": {"provider": "strong"}, "duration": {"provider": "strong"}}}
```

A continuing step must use the same provider as the step before it, and every
model must be priced in `pipeline/models.json`; planning refuses otherwise.

## 2. Plan

```sh
make experiments-plan EXPERIMENT=review-tokens
make experiments-status EXPERIMENT=review-tokens
```

Planning composes the definition over the **base run's** recipe and prompts,
validates it, and freezes the run: sources for the cohort and companions,
`prompts.json`, `recipe.json`, components, and a copy of the definition. It
reads no credentials and makes no model calls.

It then dry-runs the composed recipe against the base run's saved calls. Calls
whose requests are identical would be inherited; the others are new, priced at
their exact requests. Steps after a new call cannot be dry-run, so they are
bounded as if every unknown answer used its full output allowance. The estimate
excludes JSON-repair calls and retries, which the cap still governs.
`experiment.json` records the hypothesis, definition file hashes, base run,
cohort, overrides, batching, estimate and code revision.

## 3. Run

```sh
make experiments-run EXPERIMENT=review-tokens EXPERIMENT_ARGS='--max-spend 1.50'
```

This target loads `.env` and makes paid calls. Identical calls are copied from
the base run and cost nothing. `--max-spend` caps the experiment's cumulative
new spending; before each call the pipeline reserves its worst-case cost and
refuses it if the cap would be exceeded, so nothing is sent. Repeat the command
to resume. `--retry-failed`, `--retry-max-output-tokens`, `--workers`,
`--price-multiplier` and `--first-pass-only` behave as in `appeals.py run`.
Each execution is appended to `executions.json`.

To check a plan against real data without spending, run against an unreachable
endpoint with a tiny cap: `APPEALS_API_BASE_URL=http://127.0.0.1:9
APPEALS_API_KEY=x python appeal_runner.py run NAME --max-spend 0.000001`.

## 4. Finalize

```sh
make experiments-finalize EXPERIMENT=review-tokens
make experiments-view
```

Finalizing publishes `baseline/` and `candidate/` artifacts for the same cases,
with inherited and new costs separated, and `comparison.json` with each case's
verdict, the impact summary, status transitions, observed batching and cost.
Comparisons stay unverified until reference labels exist. Finalized artifacts
are never replaced; a finalized experiment cannot run again.

## Retrospective comparisons

```sh
make experiments-compare EXPERIMENT=same-prompt-rerun EXPERIMENT_ARGS='\
  --base-run data/appeals/release-check \
  --candidate-run data/appeals/sanity-two-group-live-20260930 \
  --hypothesis "Re-running review with identical prompts reproduces the same answers"'
```

This records two existing runs of the same frozen cases as an experiment with
no model calls. The recipe differences between the runs are recorded, but the
comparison is not controlled: code and model behavior may also differ.

## Layout

```text
experiments/NAME/          definition (git)
data/experiments/NAME/     run data (not in git)
  experiment.json          plan record
  definition/              copy of the definition when planned
  executions.json
  run/                     appeals-1 run: manifest, recipe, prompts, components, sources, calls, results
  baseline/ candidate/     finalized artifacts
  comparison.json
```

`make experiments-test` runs the runner, pipeline, dashboard and replay tests
with fake HTTP responses.
