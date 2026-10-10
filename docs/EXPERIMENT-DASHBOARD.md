# Experiment dashboard

The dashboard shows how each experiment changed the data quality of its
cases. Start it from the repository:

```sh
make experiments-view
# Appeal dashboard: http://SERVER:8771/dashboard
```

`DASHBOARD_PORT=8772` selects another port; `LISTEN_HOST=127.0.0.1` restricts
it to this machine; `DASHBOARD_ARGS='--experiments DIR'` lists another
experiment directory (default `data/experiments`). The server uses Python's
standard library and plain HTML, CSS and JavaScript. It reads saved runs,
experiment records and pipeline descriptors only: no API key, model call, OCR
pass, or run launch.

## Measuring impact on research facts

Comparing raw JSON counts every changed quotation or sentence as a changed
answer. The dashboard instead compares named research facts, defined with the
output contract (`appeal_schema.py`) and compared by `appeal_impact.py`,
grouped by research question:

| Question | Facts |
| --- | --- |
| Scope | disposition, municipal appeal, housing |
| Duration | appeal duration, start date, end date |
| Decision & status | municipal decision and date, litigation and permission status |
| Discretion | involved, central |
| Appellants | count, representation |
| Project | municipality, proposed units and lots |
| Effects, links | restriction and housing-change counts, related dockets |

Uncataloged top-level fields from another producer become generic facts;
prose, descriptions, notes and evidence never count as answers. Municipality
names are compared after removing "Town of" and ", Vermont"-style wording.
Records are matched by normalized docket within a case.

Each fact change has a direction where one is observable:

- **More specific**: unknown became an answer, or an answer became more
  precise (an exact duration instead of a minimum, an exact date instead of
  a bound).
- **Less specific**: the reverse.
- **Different answer**: two different known answers; the direction needs
  judgment.
- **Newly measured**: the candidate's schema version introduced the fact and the
  baseline's did not; this is neither better nor worse.

Each case also gets a **verdict**: *Improved* (recovered processing, gained
records or more specific answers), *Worsened* (broke, lost records or became
less specific), *Mixed*, *Needs judgment* (only different answers or a
disposition change), or *Unchanged* (wording or citations may differ). Records
that appear or disappear because the disposition changed need judgment rather
than counting as better or worse.

Without reference labels every verdict is **unverified**: a more specific
answer can be wrong, and an unknown can be correct. When a reference is
supplied, scored labels mark facts as verified correct or verified
regressions, and those override the unverified direction.

## Views

- **Experiments** lists every experiment with its hypothesis, the steps it
  changed, case counts, an impact bar and spending. Planned experiments show
  their estimate. The production baseline and the synthetic fixture are listed
  separately, followed by each pipeline tag's history: which pipeline it named,
  since when, why, and the experiment that justified the move. Experiment
  records from before the pipeline redesign stay on disk but are not listed.
- **Impact** shows the outcome bar for the selected cohort (segments open the
  filtered case list), batch companions separately, a quality scorecard
  (processing, uncertain dispositions, records, known and exact durations,
  facts answered, evidence warnings, unverified quotations, answer stability,
  reference accuracy), and *Where answers moved*:
  each fact's distribution of changes.
- **Cases** is a queue ordered worst first: *Case | What changed | Assessment*.
  Filter by verdict, cohort or companions, or search.
- **Case detail** explains the verdict, compares processing, disposition and
  its reason, errors and unverified quotations on both sides, and lists each
  record's facts (changed facts first) with evidence buttons for each side.
  Records found on only one side are listed separately.
- **What changed** draws the experiment's pipeline as a tree from its
  descriptor: each step's plain-text description, type, model, selector,
  conversation continuation and affected facts, with composite steps holding
  their children. Added, changed and removed steps are highlighted. Selecting a
  step shows its full configuration and code hashes, a baseline-versus-candidate
  table of changed fields, which source files changed, a line diff of each
  changed prompt, and the full text of the others. Samples and batching changes
  are listed below the tree.
- **Cost** separates new spending, the planned estimate, reused calls and cost
  per improved case, and lists each execution.
- **Production baseline** (`data/runs/production`) shows a quality profile: how
  often each fact is answered, least complete first, processing and
  disposition counts, unverified quotations, and its pipeline as a tree. Its
  case browser filters by status, disposition and unknown durations, and offers
  the equivalent `make experiments-cohort` command for the filters.

Views have their own URLs (`#/x/experiment:NAME/cases?verdict=worsened`), so
the browser's back button and links work.

## Evidence

Evidence buttons open the verified frozen source in a drawer. Quotations are
matched with flexible whitespace, because extracted text wraps lines, and are
highlighted. A quotation that is not verbatim in the source falls back to its
cited document page, which is marked instead. Source text is only shown when
its SHA-256 matches the frozen hash.

## Inspect supplied artifacts

```sh
python3 appeal_dashboard.py --baseline /path/to/baseline \
  --candidate /path/to/candidate --reference /path/to/reference.json
```

Baseline and candidate are run directories (`pipeline.runs.load`); the
optional reference file must satisfy `read_reference()`. Omit the candidate to
browse one run's quality profile. Experiments created with
`appeal_runner.py plan`, `finalize` or `compare` are listed automatically.

## Data access and verification

The API returns compact paginated case summaries and fetches case details and
source text on demand. Loaded runs are cached with file-change detection.
Experiment names must be plain directory names under the experiment directory.
Source requests select a case and run side; clients cannot request arbitrary
filesystem paths. Source text resolves under the run's corpus `sources`
directory, symlinks are refused, and text is only shown when its hash matches.

The server listens on all interfaces by default, for use over the private
network. It accepts `Host` headers naming `localhost` or an IP address; DNS
rebinding needs a hostname, so other names are refused unless passed with
`--allowed-host NAME` (for example through `DASHBOARD_ARGS`). An `Origin`, when
present, must match the requested host. There is no login: this internal
viewer displays source prose and evidence, so do not expose its port beyond the
private network. It is separate from the public research MCP's restricted
structured projection.

Run the backend checks with `make experiments-test`. See
[evaluation-framework.md](evaluation-framework.md) for reference scoring and
[EXPERIMENT-RUNNER.md](EXPERIMENT-RUNNER.md) for producing experiments.
