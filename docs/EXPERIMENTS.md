# Extraction experiments

Experiments measure how a change to the extraction pipeline affects data
quality. Each one reruns a frozen set of cases with explicit changes, compares
their research facts with the production baseline, and keeps the original run
untouched. The infrastructure is complete; the next work is choosing questions
and designing experiments.

## How an experiment works

A pipeline is a tree of steps built by a Python module ([pipeline](PIPELINE.md));
every pipeline produces one result per case under the output contract
([contract](../PIPELINE_CONTRACT.md)). An experiment is a module that takes the
baseline pipeline's tree and returns a modified one: a changed prompt, model or
setting, a replaced or added step, a different grouping. It never edits the
baseline's files.

```text
experiments/NAME/ (definition, in git) ─plan─▶ frozen pipeline + run ─run─▶ ─finalize─▶ dashboard
  hypothesis, cohort, experiment.py          free; dry-run estimate     paid, capped    baseline vs candidate
```

1. **Define.** `make experiments-init` creates `experiments/NAME/`; add a cohort
   with `make experiments-cohort` (the dashboard's baseline case browser shows
   the command for its filters), then edit `experiment.py` and add any step
   files or prompts.
2. **Plan.** `make experiments-plan` builds and validates the tree, freezes it
   as a pipeline ID, adds batch companions, and estimates spending with a dry
   run. It reads no credentials and makes no model calls.
3. **Run.** `make experiments-run` is the only paid step and the only target
   that loads `.env`. Identical calls, including the production run's, are
   reused for free; `--max-spend` caps new spending, and a call is refused
   before sending if it could exceed the cap. Repeating the command resumes.
4. **Finalize.** `make experiments-finalize` seals the run and compares each
   case's result with the baseline's in `comparison.json`.
5. **Review.** `make experiments-view` shows each experiment's impact on
   research facts, case by case, with verified source evidence, and its
   pipeline as a tree with the changed steps highlighted.
6. **Adopt.** `appeal_runner.py tag production p-… --experiment NAME` moves the
   `production` tag; the tag log keeps its history and the evidence.

`make experiments-compare` records two existing runs as a retrospective
comparison, with no model calls. Details: [runner](EXPERIMENT-RUNNER.md),
[pipeline](PIPELINE.md), [dashboard](EXPERIMENT-DASHBOARD.md),
[evaluation](evaluation-framework.md), [output contract](../PIPELINE_CONTRACT.md).

## Designing an experiment

1. **Pick a question and the facts it should move.** The baseline's quality
   profile shows where answers are missing. Each step's descriptor lists the
   research facts it affects; the dashboard shows them on the pipeline tree.
2. **Choose a cohort and save its query.** Small cohorts are cheap but noisy.
   Keep batch companions unless grouping is the variable.
3. **Change one thing.** Change the step that produces the facts, as late in
   the tree as possible: steps before it are reused for free, while a change to
   triage reruns every later step for every case. A change to the extraction or
   triage prompt also changes review's requests, which include them. State the
   expected effect in the hypothesis and README; plan it and check the estimate
   and the *What changed* tree before spending.
4. **Measure the noise floor on the same cohort.** Identical prompts change
   some answers from run to run. A definition that returns the base tree and
   asks for another `samples` of the changed steps measures that variation on
   the same cases; an experiment's effect should clearly exceed it.
5. **Judge against evidence.** Verdicts are unverified without reference
   labels. Read the worsened and needs-judgment cases against their sources.
6. **Adding a research field** is a schema version plus a step that fills it,
   ideally a separate step late in the tree so earlier calls are reused
   ([contract](../PIPELINE_CONTRACT.md#fact-catalog-and-schema-versions)).

## Components

| Module | Role |
| --- | --- |
| `pipeline/steps.py`, `selectors.py`, `cases.py`, `engine.py` | Step trees, selectors, case state and execution |
| `pipeline/legacy/` | The production pipeline as steps, with its prompts; conversion of legacy runs |
| `pipeline/store.py`, `providers.py`, `models.json` | Content-addressed calls, spending cap and retries; providers and prices |
| `pipeline/registry.py`, `tags.jsonl` | Pipeline IDs, frozen snapshots, diffs and tags |
| `pipeline/runs.py`, `corpus.py` | Run directories; the frozen source text |
| `appeal_schema.py` | The output contract, its validation and fact catalog |
| `appeal_cohorts.py` | Saved case selections |
| `appeal_runner.py` | Initialize, plan, run, finalize, compare, tag; production replay and legacy conversion |
| `appeal_impact.py` | Fact-level comparison, case verdicts, quality measures, baseline profile |
| `appeal_evaluation.py`, `appeal_reference.py` | Scoring against reference labels |
| `appeal_dashboard.py`, `dashboard.*` | Read-only experiment dashboard |
| `appeal_demo.py` | Synthetic fixture with reference labels, for software tests |
| `appeals.py`, `appeal_viewer.py`, `appeal_assembly.py` | Export, check and browse legacy run directories |

## Data

| Path (git-ignored) | Content |
| --- | --- |
| `data/production-import-UazDoN/data/appeals/full-run` | Production run: frozen prompts, sources (the corpus), calls, checkpoints, exports; never modified |
| `data/runs/production` | Production rebuilt by the ported pipeline from its saved calls; the dashboard's baseline and the source for cohorts |
| `data/runs/production-converted` | Production's own outputs converted to the contract; `production/acceptance.json` compares the two |
| `data/runs/release-check`, `sanity-two-group-live-20260930`, `final-smoke-20260929-190157` | Earlier legacy runs, converted |
| `data/calls` | Content-addressed calls; `index/production.json` points at production's saved calls |
| `data/pipelines` | Frozen pipeline descriptors and source copies |
| `data/experiments/NAME/` | Experiment records, runs and comparisons |
| `data/experiments/production-baseline*`, `same-prompt-rerun`, `smoke-replication` | Artifacts from before the redesign, kept but not read |
| `data/research/` and the nested `research/shared-v1` | Research MCP snapshots |
| `data/appeals/` | Earlier legacy test runs |

The checkout under `/home/coco` is authoritative. The earlier server copy in
`/home/jak/src/court-appeals-analysis` was checked on October 9, 2026: its
`full-run` had the same 19,698 files and a matching content fingerprint.

The production baseline has 1,395 cases: 953 excluded, 325 in scope and 117
uncertain (the 5 scope flags are uncertain); 1,385 complete and 10 failed
reviews. It has 513 docket records, 131 of them in uncertain cases. 54 of its
13,447 quotations are not found on their cited page.

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

Both comparisons are saved as `data/experiments/same-prompt-rerun-v2` and
`smoke-replication-v2`. Recreate them elsewhere without model calls, after
converting the legacy runs with `appeal_runner.py import-legacy`:

```sh
make experiments-compare EXPERIMENT=same-prompt-rerun-v2 EXPERIMENT_ARGS='--base-run data/runs/release-check \
  --candidate-run data/runs/sanity-two-group-live-20260930 --hypothesis "Identical prompts reproduce answers"'
make experiments-compare EXPERIMENT=smoke-replication-v2 EXPERIMENT_ARGS='--base-run data/runs/final-smoke-20260929-190157 \
  --candidate-run data/runs/production --hypothesis "Identical prompts reproduce answers"'
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
3. **Promotion.** Adopting a change moves the `production` tag, recording the
   experiment that justified it; a full run is an experiment with
   `"cohort": "all"`. Still open: the research snapshot builder reads legacy
   run directories and does not yet read the output contract.
4. **Alternative models and steps.** Experiments can swap models, prompts,
   conversations, grouping and whole subtrees. Other models need a price in
   `pipeline/models.json`; a decision model such as Jev needs a step that asks
   it typed questions ([pipeline](PIPELINE.md)).
5. **Research MCP audience.** Wider access needs decisions on isolation, rate
   limits, access policy and which fields suit bulk download.
6. **Shared validation.** The pipeline still uses helpers in `group_scout.py`;
   moving them into focused modules is optional.

## Operating notes

- `.env` holds `APPEALS_API_BASE_URL`, `APPEALS_API_KEY` and `APPEALS_MODEL`.
  Only `make experiments-run` needs it. A step's model with another `env`
  prefix reads that prefix's variables instead.
- `make test` includes a replay of the production run through the ported
  pipeline (sampled; `REPLAY_ALL=1` for every case) that requires every result
  to match production's, with sending disabled. It makes no model calls.
- `make test` runs the offline suite with mocked transports. Seven text-pipeline
  tests skip without Poppler, which runs in the container image instead.
- To check a plan against real data without spending, run it against an
  unreachable endpoint with a tiny cap (see the runner guide).
- Container targets (`make text-*`, `review`, `claims`) use Docker commands;
  on this server they need `DOCKER_HOST` pointing at the rootless Podman socket.
