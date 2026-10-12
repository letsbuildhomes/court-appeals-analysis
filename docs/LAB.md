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
names it), or on the `--confirm` keys.

## 3. Plan, then run the probe

```sh
make lab-plan EXPERIMENT=strict-review                     # changes and a dry-run cost estimate; no calls
make lab-run-send EXPERIMENT=strict-review MAX_SPEND=0.50  # the probe round, capped
```

A round runs the candidate on its keys and compares its results with the
baseline's: production's own results when an existing run covers those
cases, otherwise the baseline run from the cache. It saves the comparison in
`experiments/NAME/rounds/ROUND.json` and every step's output, so the web app
shows where the change took effect.

Rerunning a round continues it: finished work comes from the cache, and
`--resend-failed` resends failed deliveries once. `--resamples N` lets a step
try up to N further samples when it rejects an answer.

## 4. Confirm and adopt

```sh
make lab-run-send EXPERIMENT=strict-review ROUND=confirm MAX_SPEND=2.00
make lab-adopt EXPERIMENT=strict-review
```

Adopting appends a `production` entry to `pipelines/tags.jsonl` with the
confirm round's evidence. It refuses when cases regressed or did not finish;
`ARGS='--accept-regressions --note "why"'` adopts anyway, on the record.

## Batching

The September review step reviews cases in pairs. Run on a subset, cases
are paired differently than in production, so their review calls are new
even when nothing about review changed. A cached run reports those cases as
not run; a sending run pays for them. Experiments on review should keep this
in mind, and the reference set's batches should be fixed once.

## Other tools

```sh
make web                                     # Data: PDF pages beside their text; cases with grouping flags
python -m court report grouping september-cases
python -m court report case-diff september-cases d-...
python -m court dataset diff A B             # any two datasets of a kind, item by item
python -m court pipeline diff production p-...
```
