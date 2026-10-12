# Release cleanup and regression check

> Historical record. The code this describes has been replaced and its commands no longer run; see [ARCHITECTURE.md](ARCHITECTURE.md) for the current system.

The supported path is now `appeals.py`: frozen whole-group inputs → triage and
extraction → separately primed review → duration bounds → validated export.
The six selected prompts are in `prompts/appeals/`. Experiment runners are not
dependencies. The read-only viewer shows final records, review notes, duration
bounds, frozen source pages and original PDF links.

The cleanup adds resumable, budgeted calls, immutable request/response logs,
stable batch membership, and an integrated `appeals-1` export. It does not make
substantive prompt changes or manually patch model findings. During regression,
one malformed extraction required a single logged syntax-only repair. Citation
page-correction metadata also exposed a revalidation bug; the validator now
normalizes that metadata while retaining warnings and original drafts.

## Fresh model batch

`data/appeals/release-check/` contains all inputs, responses and outputs.
Eight groups completed, yielding nine docket records. Four groups were confirmed
in scope, two retained with uncertain housing scope, and two excluded. All eight
scope decisions match the historical expectations. All eight overlapping duration
records match the previous experiment; the additional Burkett duration was checked
against its source filing and disposition dates.

| Docket | Duration | Endpoint classification |
|---|---:|---|
| 101-5-07 Vtec | At least 757 days | Start on/before; end on/after |
| 293-12-08 Vtec | At least 364 days | Start on/before; end on/after |
| 231-11-09 Vtec | 153 days | Both exact |
| 150-10-14 Vtec | At least 388 days | Exact start; end on/after |
| 24-ENV-00057 | At least 83 days | Start on/before; exact end |
| 116-6-09 Vtec | At least 147 days | Start on/before; end on/after |
| 117-6-09 Vtec | At least 147 days | Start on/before; end on/after |
| 106-10-18 Vtec | At least 357 days | Exact start; end on/after |
| 75-4-06 Vtec | 272 days | Both exact |

Rice (`104-6-03 Vtec`, existing-building alteration) and Windham
(`23-ENV-00112`, Act 250 only) were excluded. KCS and Burkett remain uncertain
housing cases, not confirmed housing observations. These are deliberately selected
regression cases, not a representative accuracy sample.

Cost was **$0.142926612**, at non-peak rates: **333,494 input tokens and 198,522
output tokens**, including reasoning, across 21 provider calls including repair.
The earlier archive projection remains about $12.86 base / $21 with allowance,
38.76M input and 13.91M output tokens; see the research summary for assumptions.

## Remaining quality limits

- Five groups carry review flags. These include two uncertain-scope cases, six
  automatic citation-page corrections, three unmatched quotations across Atwood
  and Burkett, and an inconsistent municipal-decision date in the Atwood source.
- Some descriptive litigation-status labels still overstate closure, particularly
  the older Geddes dockets and Atwood. Their duration bounds correctly remain open;
  use the bounds for duration analysis rather than those labels.
- Discretion centrality and completeness/attribution of housing conditions remain
  judgment-sensitive. Atwood's six-of-eight allocation concerns an earlier board
  decision; it must not be presented uncritically as an appeal-caused reduction.
- Some appellant entries combine several named parties. Array length is not a
  person count. Source-backed names remain available.

The behavior profile is preserved, with sound duration results in this small
check and the same secondary-field limitations. This is readiness to run with
review flags, not certification that every extracted field is accurate.

## Software and interface checks

- Full suite: 135 tests run, one skipped, no failures. Eight focused tests cover
  caching, budget stops, repair, batching, partial exports, path validation,
  duration calculations and citation normalization.
- `python appeals.py check --name release-check` passes the saved scope/duration
  fixture with no differences and no model calls.
- A repeat run with an unreachable API endpoint made zero additional calls:
  the call count stayed 21, cost stayed identical, and the records export retained
  SHA-256 `625cf207704bb5501268021fb292940a561f727a86b991d805b6509316f2c94b`.
- Browser checks confirmed completed totals, scope and exact-duration filters,
  case search, readable narrow layout, keyboard activation of the evidence drawer,
  full source text and a page-specific original PDF link.

Research deletion was initially prepared as a separate action. The subsequently
authorized [cleanup](APPEALS-DELETION-PLAN.md) removed the final inventory of
1,496 obsolete files (31.49 MiB) on September 29, 2026. Original PDFs, databases,
supported code, selected prompts and release results remain preserved.

## Dashboard redesign

The viewer now separates a paginated archive/run table from research detail pages.
All 1,395 archive groups are visible, including unplanned groups. Live progress
uses processing checkpoints and the run lock, not stale final-export totals.
Research review flags remain distinct from processing failures. Details prioritize
duration, appellant roles/relief, discretion and housing consequences, with source
explanations and timelines available beneath the answers.

Four monitoring tests cover live completion before export, interrupted stages,
latest checkpoint selection, and archive/latest-run coverage. The full suite ran
142 tests without failures, one skipped. An offline release replay retained the
same records hash, 21 calls and $0.142926612 cost. Browser checks covered pagination,
scope/search controls, exact and minimum duration, multi-docket switching,
uncertain and excluded cases, original document links, the evidence drawer and
480-pixel detail layout. No paid model calls were made for this redesign.
