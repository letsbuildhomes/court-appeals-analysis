# Experiment runner

An experiment is a Python module that takes a baseline pipeline's tree and
returns a modified tree. The runner runs it on a frozen cohort and compares
each case's result with the baseline's. Its definition lives in
`experiments/NAME/`, kept in git; its run data goes to `data/experiments/NAME/`,
which is not. See [the pipeline](PIPELINE.md) for steps, selectors, providers
and pipeline IDs.

```text
experiments/NAME/ ──plan──▶ data/experiments/NAME/run   (no model calls)
                             run ──▶ paid calls under a cap on new spending
                             finalize ──▶ comparison.json
```

## 1. Create a definition

```sh
make experiments-init EXPERIMENT=review-tokens EXPERIMENT_ARGS='\
  --hypothesis "A larger review output allowance resolves output-limit failures"'
make experiments-cohort COHORT_ARGS='experiments/review-tokens/cohort.json \
  --name review-failures --description "All review failures" --status failed'
```

`init` writes `experiment.json`, an `experiment.py` that returns the base tree
unchanged, and a `README.md` to fill in. `--baseline` names the pipeline to
start from and compare with: a tag (default `production`) or a pipeline ID.

A cohort (`appeal-cohort-2`) saves its query and the case IDs and source hashes
it selected from a run (default `data/runs/production`). Query fields combine
with AND; repeated values are alternatives: `--case`, `--status` (complete,
failed, in_progress), `--disposition` (in_scope, uncertain, excluded),
`--duration-kind`. `--sample N --seed S` takes a reproducible sample. Existing
cohort files are never replaced. `"cohort": "all"` in `experiment.json` selects
every corpus case, which is how a full run is made.

### Definition files

| File | Purpose |
| --- | --- |
| `experiment.json` | `hypothesis`, `baseline`, `cohort`, and the options below |
| `experiment.py` | `build(base)` returns the experiment's tree |
| `*.py` | One file per step class the experiment adds, imported relatively |
| `prompts/*.md` | Prompts those steps load |
| `cohort.json` | The frozen case selection |
| `README.md` | Why, what changes, and how to judge it |

`experiment.json` options: `companions` (default `true`) adds the cases that
shared a call with a cohort case in the baseline run, so batched requests keep
their context; `allow_batch_change` accepts a batch membership change, which
then counts as a changed variable; `samples` maps step paths to a sample number,
calling those steps' requests afresh (see below).

Trees are values: `replace`, `remove`, `insert` and `update` return new trees.

```python
# experiments/review-tokens/experiment.py
def build(base):
    review = base.find('finishing/review')
    duration = base.find('finishing/duration')
    tree = base.update('finishing/review', model=review.model.with_settings(max_output_tokens=131072))
    return tree.update('finishing/duration', model=duration.model.with_settings(max_output_tokens=131072))
```

```python
# experiments/strict-review/experiment.py, with strict_review.py beside it
from pathlib import Path
from pipeline.steps import Prompt
from .strict_review import StrictReview

def build(base):
    review = base.find('finishing/review')
    prompts = {**review.prompts, 'review': Prompt.load(Path(__file__).with_name('prompts')/'review.md')}
    return base.replace('finishing/review', StrictReview('review', model=review.model, prompts=prompts))
```

```python
# One review per case instead of pairs (a batching change: set allow_batch_change)
from pipeline import selectors

def build(base):
    return base.update('finishing', selector=selectors.each(requires=('draft',)))
```

A change to a prompt that several steps include (review's system prompt joins
the extraction and scope contracts) must be made in each of them;
`pipeline.legacy.build(prompt_overrides={...})` rebuilds the legacy tree with a
shared prompt replaced everywhere. Planning validates the tree: conversations
continue only within a group and with the same model, groups fit each step's
limit, and every model is priced.

### Measuring run-to-run variation

Identical requests reuse their saved answers, so rerunning an unchanged
pipeline measures nothing. A definition whose `build` returns the base and
whose `samples` asks for `{"finishing/review": 1}` calls review afresh for the
same cases; later steps pay only where the new sample's answer differs. Its
comparison is the noise floor an experiment's effect must exceed.

## 2. Plan

```sh
make experiments-plan EXPERIMENT=review-tokens
make experiments-status EXPERIMENT=review-tokens
```

Planning resolves the baseline to a pipeline ID and finds a run of it covering
the cohort, rebuilds the baseline's tree, builds the experiment's tree from it,
and freezes the experiment's pipeline. It adds batch companions, creates the
run, and copies the definition. It reads no credentials and makes no model
calls.

It then executes the tree as a dry run. Calls identical to saved ones would be
reused; the others are new, priced at their exact requests. Steps after a new
call cannot be dry-run, so they are bounded as if every unknown answer used its
full output allowance. The estimate excludes JSON-repair calls and retries,
which the cap still governs. If the cases' batches would differ from the
baseline's, planning stops unless the definition allows it.

`experiment.json` records the hypothesis, definition file hashes, baseline
(reference, pipeline ID, run, and the ID its tree rebuilds to today), candidate
pipeline ID, cohort, cases and companions, samples, batching and estimate.
`status` lists what changed between the two pipelines.

## 3. Run

```sh
make experiments-run EXPERIMENT=review-tokens EXPERIMENT_ARGS='--max-spend 1.50'
```

This target loads `.env` and makes paid calls. Identical calls, including the
production run's, are reused at no cost. `--max-spend` caps the experiment's
cumulative new spending; before each call the pipeline reserves its worst-case
cost and refuses it if the cap would be exceeded, so nothing is sent. Repeat
the command to resume: the tree re-executes and finished work is reused.
`--retry-failed` resends each failed call, or call whose answer was rejected,
once (`--retry-max-output-tokens` raises its output allowance); `--workers` and
`--price-multiplier` are also available. A run refuses to start if the
experiment's code or configuration changed since it was planned. Each execution
is appended to the run's `executions.json`.

## 4. Finalize

```sh
make experiments-finalize EXPERIMENT=review-tokens
make experiments-view
```

Finalizing seals the run and writes `comparison.json`: each case's verdict
against the baseline run, the impact summary, status transitions, observed
batching and cost (new and reused). Comparisons stay unverified until reference
labels exist. A finalized experiment cannot run again.

## 5. Adopt

```sh
uv run --inexact python appeal_runner.py tag production p-… --experiment review-tokens \
  --note "A larger review allowance recovered three failed batches"
```

The tag log keeps every earlier target, with the experiment that justified the
move. Later experiments built on `production` start from the new pipeline; its
experiment run is found as their baseline for the cases it covered.

## Retrospective comparisons

```sh
make experiments-compare EXPERIMENT=same-prompt-rerun-v2 EXPERIMENT_ARGS='\
  --base-run data/runs/release-check \
  --candidate-run data/runs/sanity-two-group-live-20260930 \
  --hypothesis "Re-running review with identical prompts reproduces the same answers"'
```

This records two existing runs of the same frozen cases as a comparison with no
model calls. The pipelines' differences are shown, but the comparison is not
controlled: model behavior may also differ. Legacy runs are converted first:

```sh
uv run --inexact python appeal_runner.py import-legacy data/appeals/release-check --name release-check
```

## Production baseline

```sh
make experiments-replay    # appeal_runner.py replay-production
```

This indexes the production run's saved calls (once), converts its outputs to
`data/runs/production-converted`, rebuilds it with the ported pipeline into
`data/runs/production` with sending disabled and a zero cap, and writes
`acceptance.json` comparing the two case by case. It makes no model calls.

## Layout

```text
experiments/NAME/          definition (git)
data/experiments/NAME/     run data (not in git)
  experiment.json          plan record
  definition/              copy of the definition when planned
  run/                     the experiment's run (see PIPELINE_CONTRACT.md)
  comparison.json
data/pipelines/<id>/       frozen pipeline descriptors and source copies
data/calls/                content-addressed calls; index/ for earlier runs' saved calls
data/runs/NAME/            standalone runs: production, converted legacy runs
pipeline/tags.jsonl        tag log (git)
```

`make experiments-test` runs the runner, pipeline, store, dashboard and replay
tests with fake responses.
