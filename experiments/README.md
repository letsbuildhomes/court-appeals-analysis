# Experiment records

Each directory is one experiment: `experiment.json` (hypothesis, baseline,
candidate definition, starting position, inputs and round keys) and
`rounds/ROUND.json` (each round's comparison, regressions, step-by-step data
diffs and cost). The candidate pipeline lives in `pipelines/definitions/`;
its new steps and prompts live in `steps/` and `prompts/`. See
[docs/LAB.md](../docs/LAB.md).
