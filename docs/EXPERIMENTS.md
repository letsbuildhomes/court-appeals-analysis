# Extraction experiments

Experiments measure how a change to the extraction pipeline affects data
quality. Each one reruns a frozen set of cases with explicit changes, compares
their research facts with the production baseline, and keeps the original run
untouched. The infrastructure is complete; the next work is choosing questions
and designing experiments.

## How an experiment works

The pipeline is a recipe of steps, prompts, providers and batching
([pipeline](PIPELINE.md)). An experiment is a directory of files composed over
the base run's recipe: a recipe fragment, replacement prompts, or new step
components. It never edits the base files.

```text
experiments/NAME/ (definition, in git) ─plan─▶ frozen run ─run─▶ ─finalize─▶ dashboard
  hypothesis, cohort, overrides          free; dry-run estimate  paid, capped   baseline vs candidate
```

1. **Define.** `make experiments-init` creates `experiments/NAME/`; add a cohort
   with `make experiments-cohort` (the dashboard's baseline case browser shows
   the command for its filters), then any `recipe.json`, `prompts/` or
   `components/` overrides.
2. **Plan.** `make experiments-plan` composes and validates the recipe, freezes
   the run, adds review-batch companions, and estimates spending with a dry run.
   It reads no credentials and makes no model calls.
3. **Run.** `make experiments-run` is the only paid step and the only target
   that loads `.env`. Identical saved calls are inherited for free;
   `--max-spend` caps new spending, and a call is refused before sending if it
   could exceed the cap. Repeating the command resumes.
4. **Finalize.** `make experiments-finalize` publishes baseline and candidate
   artifacts for the same cases and `comparison.json`.
5. **Review.** `make experiments-view` shows each experiment's impact on
   research facts, case by case, with verified source evidence, and what it
   changed in the pipeline.

`make experiments-compare` records two existing runs as a retrospective
comparison, with no model calls. Details: [runner](EXPERIMENT-RUNNER.md),
[pipeline](PIPELINE.md), [dashboard](EXPERIMENT-DASHBOARD.md),
[evaluation](evaluation-framework.md), [frozen-text contract](../PIPELINE_CONTRACT.md).

## Designing an experiment

1. **Pick a question and the facts it should move.** The baseline's quality
   profile shows where answers are missing. [The pipeline guide](PIPELINE.md)
   maps each research fact to the step and prompts that produce it.
2. **Choose a cohort and save its query.** Small cohorts are cheap but noisy.
   Keep batch companions unless batching is the variable.
3. **Change one thing.** Write the override files and state the expected effect
   in the hypothesis and README. Plan it and check the estimate and the
   *What changed* view before spending.
4. **Measure the noise floor on the same cohort.** Identical prompts change
   some answers from run to run. A second definition with no overrides and
   `"rerun"` listing the changed steps measures that variation on the same
   cases; an experiment's effect should clearly exceed it.
5. **Judge against evidence.** Verdicts are unverified without reference
   labels. Read the worsened and needs-judgment cases against their sources.

## Components

| Module | Role |
| --- | --- |
| `recipes/appeals/recipe.json`, `prompts/appeals/` | The production recipe and its prompts |
| `pipeline/` | Recipe composition and validation, engine, step components, batchers, conversations, providers and model prices |
| `appeals.py`, `appeal_runtime.py` | Plan and run pipeline runs; the call layer with spending caps, reuse and read-through |
| `appeal_contracts.py`, `appeal_assembly.py` | Read a saved run as canonical cases; checkpoint and record assembly |
| `appeal_experiments.py` | Immutable baseline and candidate artifacts, optionally restricted to a cohort |
| `appeal_cohorts.py` | Saved case selections |
| `appeal_runner.py` | Initialize, plan, run, finalize and compare experiments |
| `appeal_impact.py` | Fact-level comparison, case verdicts, quality measures, baseline profile |
| `appeal_evaluation.py`, `appeal_reference.py` | Scoring against reference labels |
| `appeal_dashboard.py`, `dashboard.*` | Read-only experiment dashboard |
| `appeal_demo.py` | Synthetic fixture with reference labels, for software tests |

## Data

| Path (git-ignored) | Content |
| --- | --- |
| `data/production-import-UazDoN/data/appeals/full-run` | Production run: frozen prompts, sources, calls, checkpoints, exports |
| `data/experiments/production-baseline-relocated` | Its canonical artifact; the dashboard's production baseline and the source for cohorts |
| `data/experiments/production-baseline` | The same artifact as created on the laptop; its source paths point there, so it cannot show evidence |
| `experiments/NAME/` (in git) | Experiment definitions |
| `data/experiments/NAME/` | Experiment runs: plan, executions, run, finalized artifacts |
| `data/research/` and the nested `research/shared-v1` | Research MCP snapshots |
| `data/appeals/` | Earlier test runs: `release-check`, `sanity-two-group-*`, `final-smoke-*` |

The checkout under `/home/coco` is authoritative. The earlier server copy in
`/home/jak/src/court-appeals-analysis` was checked on October 9, 2026: its
`full-run` had the same 19,698 files and a matching content fingerprint.

The production baseline has 1,395 source groups: 1,380 complete (including 953
exclusions), 10 review failures and 5 scope flags. It has 513 finalized docket
records, 382 confirmed and 131 uncertain in scope.

- **Review failures (10 groups in 6 review batches):** three batches ended at
  the output limit (`g-134`/`g-135`, `g-17`/`g-170`, `g-222`/`g-223`). One
  batch's response did not cover each member exactly once (`g-11`/`g-110`).
  `g-209-11-04-vtec` cited a nonexistent page; `g-50-3-07-vtec` had an
  invalid changes/flags structure. The companions `g-208-9-08-vtec` and
  `g-5-1-17-vtec` completed in those batches.
- **Scope flags (5 groups):** `g-106-6-03-vtec`, `g-195-9-07-vtec`,
  `g-238-11-05-vtec`, `g-25-env-00010`, `g-73-2-02-vtec`. Two may upgrade
  unclear housing scope; three involve inappropriate docket inclusion or
  enforcement and home-business proceedings. Resolve them from source
  evidence; a flag is not an ordinary retry.

## What is established

- **Where answers are missing.** The dashboard's production profile ranks facts
  by completeness. Least complete: proposed lots (21%), related dockets (43%),
  discretion central (50%), proposed units (58%), decision date and discretion
  involved (61%), and appeal duration (74%; 135 records unknown). An unknown
  can be the correct answer.
- **Run-to-run variation.** Retrospective comparisons of runs with identical
  prompts changed 4–11% of compared facts. Litigation status was the least
  stable, and one rerun broke a previously complete review. An experiment's
  changes need to exceed this before they mean anything; repeat a
  same-prompt comparison when judging a new effect.
- **The earlier prompt revision.** It replaced one word in the extraction
  prompt. Its differences on the release-check cases look like run-to-run
  variation, not an effect of the edit.

Both comparisons are saved as `data/experiments/same-prompt-rerun` and
`smoke-replication`. Recreate them elsewhere without model calls:

```sh
make experiments-compare EXPERIMENT=same-prompt-rerun EXPERIMENT_ARGS='--base-run data/appeals/release-check \
  --candidate-run data/appeals/sanity-two-group-live-20260930 --hypothesis "Identical prompts reproduce answers"'
make experiments-compare EXPERIMENT=smoke-replication EXPERIMENT_ARGS='--base-run data/appeals/final-smoke-20260929-190157 \
  --candidate-run data/production-import-UazDoN/data/appeals/full-run --hypothesis "Identical prompts reproduce answers"'
```

## Designing experiments: open decisions

1. **First questions.** Candidates: recovering the 10 review failures (an
   identical retry against a larger review output allowance), and recovering
   unknown durations or decision dates. Save the cohort query, keep batch
   companions or record batching as a changed variable, and compare against
   run-to-run variation.
2. **Reference labels.** Verdicts stay unverified until reviewed labels exist.
   [The selection proposal](reference-selection-proposal.md) lists the
   decisions: eligible population, representative and challenge samples,
   pilot size, field rubric, reviewer and adjudicator process, development and
   held-out split, and a spending cap. A model-produced record is a candidate
   label until independently reviewed.
3. **Promotion.** Define how accepted experiment results compose a release:
   source hashes, code revision, prompt, configuration and model identity,
   selected cases, cost and explicit result selection. The research snapshot
   builder still selects a single run.
4. **Alternative models and steps.** Recipes can swap providers, prompts,
   conversation modes, batching and step components. Other models need a price
   in `pipeline/models.json`; a decision model such as Jev needs a component
   that asks it typed questions ([pipeline](PIPELINE.md)). A recipe still runs
   one per-case phase then one batched phase.
5. **Research MCP audience.** Wider access needs decisions on isolation, rate
   limits, access policy and which fields suit bulk download.
6. **Shared validation.** The pipeline still uses helpers in `group_scout.py`;
   moving them into focused modules is optional.

## Operating notes

- `.env` holds `APPEALS_API_BASE_URL`, `APPEALS_API_KEY` and `APPEALS_MODEL`.
  Only `make experiments-run` and direct `appeals.py run` need it. A recipe
  provider with another `env` prefix reads that prefix's variables instead.
- `make test` includes a replay of the production run through the recipe
  engine (sampled; `REPLAY_ALL=1` for every case) that requires every model
  request to match the saved one. It makes no model calls.
- `make test` runs the offline suite with mocked transports. Seven text-pipeline
  tests skip without Poppler, which runs in the container image instead.
- To check a plan against real data without spending, run it against an
  unreachable endpoint with a tiny cap (see the runner guide).
- Container targets (`make text-*`, `review`, `claims`) use Docker commands;
  on this server they need `DOCKER_HOST` pointing at the rootless Podman socket.
