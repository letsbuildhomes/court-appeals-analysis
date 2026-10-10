# Experiment definitions

Each directory here defines one experiment as a Python module that modifies a
baseline pipeline's tree; the baseline's files are never edited:

```text
NAME/
  experiment.json   hypothesis, baseline (tag or pipeline ID), cohort, options (companions, allow_batch_change, samples)
  experiment.py     build(base) returns the experiment's tree
  *.py              one file per step class it adds, imported relatively (from .strict_review import StrictReview)
  prompts/*.md      prompts those steps load
  cohort.json       frozen case selection, or "cohort": "all" in experiment.json
  README.md         why, what changes, how to judge it
```

Start one with `make experiments-init EXPERIMENT=NAME EXPERIMENT_ARGS='--hypothesis "..."'`.
Run data goes to `data/experiments/NAME/`, outside git. See
[docs/EXPERIMENTS.md](../docs/EXPERIMENTS.md) and
[docs/EXPERIMENT-RUNNER.md](../docs/EXPERIMENT-RUNNER.md).
