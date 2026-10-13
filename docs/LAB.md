# Lab guide

The lab tests a change to the pipeline and compares what it produces. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the concepts; this guide is the
commands. Everything here runs through `python -m court` (`make` targets are
shortcuts). Only commands given `--send` make paid model calls; they need
`--max-spend` and read credentials from the environment (`make ...-send`
targets pass `.env`).

## The starting point

`production` names the September pipeline, reproduced from its saved data:

```sh
python -m court tags                       # tag history
python -m court pipeline show production   # the step tree, with settings and prompts
python -m court runs                       # runs, including "september" over every case
make web                                   # http://SERVER:8771/: production's quality profile, live runs
```

## 1. Make the change

A change is new library entries plus a pipeline definition:

- A new prompt goes in `prompts/FAMILY/VARIANT.md`, for example
  `prompts/review/strict-citations.md` (`review:strict-citations`).
- New code goes in `steps/FAMILY/VARIANT.py`: one file, one step. Copy the
  variant you are changing and edit the copy; never edit an entry a
  pipeline has used (a test checks).
- Settings changes need no new files.

## 2. Create the experiment

```sh
python -m court lab new strict-review \
  --hypothesis 'Requiring a quote for every change stops unsupported edits' \
  --at extract --input cases=september-cases \
  --keys g-50-3-07-vtec,g-209-11-04-vtec \
  --confirm reference --reference reference \
  --changes '[{"at": "extract/review", "prompts": {"review": "review:strict-citations"}}]'
```

This writes `experiments/strict-review/experiment.json` and the candidate's
definition, `pipelines/definitions/strict-review.json`: the baseline's ID plus
the changes. Edit that file to change the candidate; positions are slash
paths such as `extract/review`, and changes can set `settings` or `prompts`,
swap the `step`, `replace`, `insert` or `remove` a node.

`--at` is where the experiment's runs start, with `--input` giving that
step's datasets. The probe keys (`--keys`) are the cases the change targets.
The confirm round runs on the reference set (once one exists; `--reference`
names it), or on the `--confirm` keys. A confirm round over every case
(`--confirm all`) leaves the complete run the next experiment compares with
once this one is adopted.

### Changes before extraction

A change to grouping or text has to run from earlier in the pipeline, but
its cases are still compared at extraction. `--focus` names the position the
keys apply to: every step before it runs on everything, and only the focused
step is limited. Earlier steps make no model calls and come from the caches,
so the whole pipeline from the top takes under a minute. `--keys changed`
chooses, when the round runs, every case whose input at the focus differs
from the baseline's, plus the cases no longer there (merged or removed);
`--confirm all` runs every case.

```sh
python -m court lab new bracket-columns \
  --hypothesis 'Reading docket numbers through bracket columns groups documents more accurately' \
  --focus extract --keys changed --confirm all \
  --changes '[{"at": "cases/group", "step": "cases/group:caption-dockets-bracket-columns"}]'
```

Without `--at`, runs start at the top of the pipeline, from the listing
snapshot. Each compared case is marked by its input: changed, merged away or
removed, new, or the same as the baseline's. A case with the same input that
still changed did so by chance or because review pairs cases; it doesn't
measure the change.

## 3. Plan, then run the probe

```sh
make lab-plan EXPERIMENT=strict-review                     # changes and a dry-run cost estimate; no calls
make lab-run-send EXPERIMENT=strict-review MAX_SPEND=0.50  # the probe round, capped
```

A round runs the candidate on its keys and compares its results with the
baseline's, read from the baseline's complete run: one over every case with
every step saved, such as `september` or a confirm round's run. Nothing is
run for the baseline side. The round saves the comparison in
`experiments/NAME/rounds/ROUND.json` and every step's output, so the web app
shows where the change took effect. When the baseline has no complete run,
the round says so and gives the command for one; from the cache it is free.

A baseline that reviews one case per call (no step with `batch_cases` above
1) answers a subset exactly as it would answer everything, so it doesn't
need a complete run. `lab baseline` runs it over the experiment's reference
set, or `--keys`, and rounds on those cases read their baseline side from
that run:

```sh
python -m court lab baseline strict-review --send --max-spend 2   # the baseline over the reference set
```

Probes should be small: an explicit `--keys` list of the cases the change
targets. `--keys changed` picks every case whose input changed, which for a
change that touches many cases is nearly a full run.

A plan saves its estimate in `experiments/NAME/plans/ROUND.json`, which the
web app shows before the round runs. Its dry run is kept in `data/estimates`,
out of the runs list.

Rerunning a round continues it: finished work comes from the cache, and
`--resend-failed` resends failed deliveries once more each time, with
`--resend-max-tokens` for a larger output limit. `--max-spend` limits what
one execution spends; earlier executions of the run don't count. `--resamples N` lets a step
try up to N further samples when it rejects an answer.

## 4. Confirm and adopt

```sh
make lab-run-send EXPERIMENT=strict-review ROUND=confirm MAX_SPEND=2.00
make lab-adopt EXPERIMENT=strict-review
```

Adopting appends a `production` entry to `pipelines/tags.jsonl` with the
confirm round's evidence. It refuses when the confirm round did not finish
every case. When cases got worse, it lists them and asks whether to adopt
anyway; the tag log records them. `ARGS='--accept-regressions --note "why"'`
answers yes without asking.

## Batching

The September review step reviews cases in pairs. Run on a subset, cases
are paired differently than in production, so their review calls are new
even when nothing about review changed. A cached run reports those cases as
not run; a sending run pays for them. Pipelines after September review one
case per call (`batch_cases: 1`), which removes this and lets a baseline run
over just the reference set.

## Other tools

```sh
make web                                     # Data: PDF pages beside their text; cases with grouping flags
python -m court report grouping september-cases
python -m court report case-diff september-cases d-...
python -m court dataset diff A B             # any two datasets of a kind, item by item
python -m court pipeline diff production p-...
```
