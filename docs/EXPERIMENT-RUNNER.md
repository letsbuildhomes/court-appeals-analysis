# Experiment runner

The runner tests a change to the extraction pipeline on a frozen set of cases
and compares the result with the original run. It reuses the supported pipeline
(`appeals.py`) and its spending controls. The base run is only read.

```text
experiment artifact ──select──▶ cohort.json
base run + cohort + changes ──plan──▶ data/experiments/NAME/   (no model calls)
                                        run ──▶ paid calls under a cap on new spending
                                        finalize ──▶ baseline/ candidate/ comparison.json
```

## 1. Select a cohort

```sh
make experiments-cohort COHORT_ARGS='data/experiments/cohorts/review-failures.json \
  --from data/experiments/production-baseline-relocated \
  --name review-failures --description "All saved review failures" --status review_failed'
```

A cohort (`appeal-cohort-1`) saves the query and the identities and source
hashes it selected. Query fields combine with AND; repeated values of one
field are alternatives: `--case`, `--status`, `--scope`, and `--duration-kind`.
`--sample N --seed S` takes a reproducible random sample after filtering. An
empty query is refused, and an existing cohort file is never replaced.

## 2. Plan an experiment

```sh
make experiments-plan EXPERIMENT=review-tokens EXPERIMENT_ARGS='\
  --base-run data/production-import-UazDoN/data/appeals/full-run \
  --cohort data/experiments/cohorts/review-failures.json \
  --hypothesis "A larger review output allowance resolves output-limit failures" \
  --review-max-output-tokens 131072'
```

Planning reads no credentials and makes no model calls. Changes are recorded
as experiment variables:

| Option | Changes | Stages rerun |
| --- | --- | --- |
| `--prompt NAME=FILE` | One frozen prompt (`system`, `triage`, `extract`, `primer`, `review`, `duration`) | Stages whose requests contain it, and later stages |
| `--model`, `--reasoning`, `--max-output-tokens` | Every stage's requests | All |
| `--review-max-output-tokens` | Review and duration output allowance | Review, duration |
| `--rerun-from STAGE` | Nothing; makes new calls from this stage | That stage onward |

With no changes, an experiment is an identical retry. Saved failed calls are
never reused, so failed work is attempted again; completed work is reused.

The plan copies the cohort's frozen sources into a new `appeals-1` run under
`data/experiments/NAME/run/`. It reuses saved work from unchanged stages:
successful triage calls are copied only when a rebuilt request is
byte-identical, along with their first-pass checkpoints and successful
review or duration calls. Copied charges are inherited, not new spending.

Review sends two groups per request. A cohort group's batch companion is added
automatically so its review request has the same context; the comparison
reports companions separately. When all first passes are reused, planning
predicts the batches and refuses a membership change unless
`--allow-batch-change` records batching as a changed variable. `--no-companions`
always changes batching. Otherwise batching is checked after the run.

`experiment.json` records the hypothesis, base run and manifest hash, cohort
hash and query, variables, reuse, current code revision (including whether
the checkout has uncommitted changes), and the estimate:

- **historical** is what the base run recorded for the same groups and stages;
- **upper bound** assumes every uncached response uses its full output
  allowance and every group needs extraction. It excludes optional JSON-repair
  calls and retries, which the cap still governs.

`make experiments-status EXPERIMENT=NAME` shows the plan, progress, and spending.

## 3. Run

```sh
make experiments-run EXPERIMENT=review-tokens EXPERIMENT_ARGS='--max-spend 1.00'
```

This target loads `.env` and makes paid calls. `--max-spend` is a cumulative cap
on the experiment's new spending; inherited charges do not count. Before each
call, the pipeline reserves the call's worst-case cost and refuses it if the
cap would be exceeded, so nothing is sent. Repeat the command, with a larger
cap if needed, to resume; completed calls are reused free. `--retry-failed`,
`--retry-max-output-tokens`, `--workers`, `--price-multiplier`, and
`--first-pass-only` behave as in `appeals.py run`. Each execution is appended
to `executions.json` with its cap, code revision, spending, and status counts.

## 4. Finalize and compare

```sh
make experiments-finalize EXPERIMENT=review-tokens
make experiments-view    # lists every experiment under data/experiments
```

Finalizing publishes two `appeal-experiment-1` artifacts restricted to the same
cases: `baseline/` from the base run, with the base run's recorded cost for
those cases, and `candidate/` (`experiment_candidate`) with inherited and new
costs separated. `comparison.json` lists each case's verdict and changes (see the
[dashboard](EXPERIMENT-DASHBOARD.md) for definitions), the impact summary,
status transitions, record counts, observed batching and cost. Comparisons are unverified
until a reference is supplied; see the [evaluation framework](evaluation-framework.md).
Finalized artifacts are never replaced, and a finalized experiment cannot be
run again; plan a new experiment instead.

## Retrospective comparisons

Two existing runs of the same frozen cases can be recorded as an experiment
without any model calls, for example to see what an earlier prompt revision
changed or how much two runs with identical prompts disagree:

```sh
make experiments-compare EXPERIMENT=same-prompt-rerun EXPERIMENT_ARGS='\
  --base-run data/appeals/release-check \
  --candidate-run data/appeals/sanity-two-group-live-20260930 \
  --hypothesis "Re-running review with identical prompts reproduces the same answers"'
```

Cases default to every case both runs share with identical frozen sources;
`--cohort` selects fewer. The prompt and setting differences between the runs
are recorded as variables, but the comparison is not controlled: code and
model behavior may also differ. A same-prompt comparison measures run-to-run
variation, which is the noise floor any experiment's changes should exceed.

## Layout

```text
data/experiments/NAME/
  experiment.json      immutable plan
  executions.json      one entry per run invocation
  run/                 appeals-1 run: manifest, prompts, sources, calls, results, export
  baseline/ candidate/ finalized artifacts
  comparison.json
```

The tests (`make experiments-test`) use fake HTTP responses. To check a plan
against real data without spending, run against an unreachable endpoint with a
tiny cap, for example `APPEALS_API_BASE_URL=http://127.0.0.1:9 APPEALS_API_KEY=x
python appeal_runner.py run NAME --max-spend 0.000001`. The cap refuses every
call before sending.
