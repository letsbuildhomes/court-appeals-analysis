# Experiment dashboard

Start the local read-only viewer from the repository:

```sh
make experiments-view
# http://127.0.0.1:8771/
```

`DASHBOARD_PORT=8772` selects another loopback port. The server uses Python's
standard library and the existing project's HTML/CSS/JavaScript conventions.
No build step, API key, model call, OCR pass, or environment-file access is
needed. It does not launch runs, choose a production reference cohort, or write
experiment artifacts.

The default **software fixture** comes from `appeal_demo.build_demo()`. Its
cases, labels, costs, execution states, and recipe changes are invented to test
the framework. Its displayed scores are software-test results, not measured
research accuracy. The separate **production baseline** reads
`data/experiments/production-baseline` when available. It has no candidate or
reference labels, so the dashboard makes no production quality claim.

## Four views

- **Review** starts with regressed, improved, and unscored changed fields. Click
  a summary to narrow the case list, then open a case to compare reference,
  baseline, and candidate values. Evidence opens verified source text in a
  side drawer; returning to the list preserves filters and scroll position.
- **Pipeline** shows each recorded recipe's components and edges, configuration,
  and available frozen prompt text. Direct component changes and inherited
  prompt dependency changes are distinguishable. Recipe topology is taken from
  the artifact rather than assumed to follow one fixed pipeline.
- **Inputs** searches and filters run-local case identities, docket identities,
  scope and execution coverage. Details retain source hashes, source metadata,
  finalized records, and the synthetic fixture's selection rationale.
- **Runs & costs** describes recorded execution snapshots. Completed, failed,
  interrupted, and simulated running work remain separate. Inherited recorded
  charges, new spending, reservations, simulated values, and unknown costs are
  not interchangeable. A running fixture is not an active model request.
- Review details distinguish missing, additional and ambiguous
  records, incompatible sources, and execution failures have their own
  diagnostics. Unknown labels remain unscored, and missing output differs from
  an explicit null value. Evidence quotations open verified frozen source text.

The workspace selector switches between the software fixture and production
baseline. With only a baseline, Review presents a starting-point inventory
instead of comparison scores. Component instructions, raw artifacts, and
provenance remain available on demand rather than competing with review work.
Dropdowns share the pipeline dashboard's keyboard-accessible controls.

The same paired comparable fields form both accuracy denominators. Coverage
reports how many eligible labels could actually be compared. These are
descriptive counts; the fixture and any future selected reference set do not
establish population accuracy. See [evaluation-framework.md](evaluation-framework.md)
for scoring semantics and [../PIPELINE_CONTRACT.md](../PIPELINE_CONTRACT.md) for
the frozen-text boundary.

## Inspect supplied artifacts

To inspect a future comparison that has already been produced:

```sh
python3 appeal_dashboard.py --baseline /path/to/baseline \
  --candidate /path/to/candidate --reference /path/to/reference.json
```

Baseline and candidate directories must satisfy `read_experiment()`; the
optional reference file must satisfy `read_reference()`. Omit the reference
for an unscored output comparison, or supply only a baseline to browse it.
This command reads those artifacts; it does not produce a candidate or labels.

## Data access and verification

The API returns compact paginated case summaries and fetches case details and
source text on demand. Loaded artifacts are cached with file-change detection
and validated through the experiment reader. The server serves only explicit
dashboard assets and API routes. Source requests select a case and run side;
clients cannot request arbitrary filesystem paths. Source text resolves under
the recorded source run's sources directory and must match the case's SHA-256.
Missing or mismatched source bytes produce an explicit integrity error.

The server binds only to loopback. This local internal viewer can display
source prose and evidence; it is separate from the public research MCP's
restricted structured projection.

Run the focused backend checks with `make experiments-test`. Browser checks
should cover all four views, summary-to-case drilldown, empty searches, dataset
switching, evidence navigation, and a source-integrity error.
