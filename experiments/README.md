# Experiment definitions

Each directory here defines one experiment as files composed over a base run's
recipe; the base files are never edited:

```text
NAME/
  experiment.json   hypothesis, base_run, cohort, options (companions, rerun, allow_batch_change)
  cohort.json       frozen case selection
  recipe.json       optional recipe fragment
  prompts/*.md      optional replacement or new prompts
  components/*.py   optional new step components
  README.md         why, what changes, how to judge it
```

Start one with `make experiments-init EXPERIMENT=NAME EXPERIMENT_ARGS='--base-run ... --hypothesis "..."'`.
Run data goes to `data/experiments/NAME/`, outside git. See
[docs/EXPERIMENTS.md](../docs/EXPERIMENTS.md) and
[docs/EXPERIMENT-RUNNER.md](../docs/EXPERIMENT-RUNNER.md).
