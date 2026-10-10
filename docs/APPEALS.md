# Municipal housing appeal extraction

> **Historical guide.** This describes how the production run was made with the
> former `appeals.py` runner. That pipeline now lives as steps in
> `pipeline/legacy/` and new runs go through `appeal_runner.py`
> ([pipeline](PIPELINE.md), [runner](EXPERIMENT-RUNNER.md)). `appeals.py` keeps
> only `export`, `status`, `check` and `serve` for legacy run directories; the
> `plan` and `run` commands and `appeal_sanity.py` below no longer exist.

The production pipeline was `appeals.py`. It reads the existing docket groups and
selected PDF text, classifies scope, extracts compact records, reviews them in
small batches, and establishes evidence-backed duration bounds. It uses DeepSeek
Flash with high reasoning and a 65,536-token output allowance. No agent loop or
document-by-document intermediate model is required.

## Run

Configure an OpenAI-compatible chat-completions API directly. No proxy process is
required. The API key is read from the environment and is not saved in run logs:

```sh
export APPEALS_API_BASE_URL=https://your-provider.example/v1
export APPEALS_API_KEY=your-key
export APPEALS_MODEL=deepseek-flash

make appeals-plan APPEALS_ARGS='--name my-run --group g-150-10-14-vtec'
make appeals-run APPEALS_ARGS='--name my-run --max-spend 0.50'
make appeals-status APPEALS_ARGS='--name my-run'
make appeals-view
```

The base URL must include any provider path such as `/v1`; the pipeline appends
`/chat/completions`. Alternatively set `APPEALS_API_URL` to the complete endpoint.
Set only one URL variable. Run flags `--api-base-url` or `--api-url` also work.
The model is required at planning time through `APPEALS_MODEL` or `--model` and
is frozen in the manifest. Current cost estimates use DeepSeek Flash rates;
they are not a provider-independent billing guarantee.

Run `make appeals-view` and open the printed address, `http://SERVER:8769/` on
the server's private-network address. The homepage is a searchable, paginated table of all
archive groups. Each row shows docket identifiers, a source-document title,
document count, processing status, scope, and municipality when extracted. Select
a run to see its planned population and completion progress. All-archive mode
shows the most recent planned run for each group; it does not merge findings from
different runs. A document title identifies the group without inventing a project
name or address.

The page refreshes data every five seconds while visible. Checkpoints expose
scope checking, extraction, review, and duration stages, and completed results
appear before the run's final export. Completed scope exclusions count toward
processing progress; research uncertainty/review flags are separate. A stopped
process with an unfinished active checkpoint is shown as interrupted.

Progress has separate bars for the first pass and finalized results, with PDF
counts, percentages and group counts. PDFs are counted once by checksum. A first
pass counts only after scope checking and any required extraction succeed;
later review failures do not erase that milestone. Finalized coverage includes
reviewed exclusions. Archive coverage uses the latest planned run for each group.

Live research summaries above processing coverage use completed, confirmed
municipal housing appeal dockets in the selected archive/run view. Related dockets
count separately; group counts are shown alongside them. Uncertain-scope records
stay separate, and research-flagged records remain included with a visible count.
Table search and filters apply only to the group list. Research summaries and
charts are noninteractive; individual records are accessible through the table.
The four research panels use horizontal rows: a duration histogram and single
100% stacked bars for discretion, observed permission and representation, with
unknown categories retained. The discretionary “yes” portion uses three green
shades for primary, peripheral and primary status not established. These remain
distinct from an unknown discretionary basis. Housing-impact counts are visible
under observed permission. The duration legend sits below the histogram.

Duration means exclude missing or invalid durations; a supported zero-day interval
is valid. The supported mean combines exact intervals and minimum bounds, so the
full mean may be longer. The exact-only mean has its own sample count. Six duration
buckets use upper bounds of 90, 180, 365, 730 and 1,460 days, then over 1,460;
minimum intervals may ultimately belong in a later bucket. Missing discretionary,
permission, and representation classifications remain unknown. Empty findings
arrays mean none documented, not proof of no impact; absent arrays remain unknown.
Representation is counted once per appeal and is incomplete if any appellant's
status is missing. Changes and restriction categories can overlap.

Group details start with the project and challenged municipal decision, followed
by four research questions: appeal duration, appellants and requested relief,
discretionary basis/centrality, and observed housing permission/changes/restrictions.
Multiple dockets have separate tabs; durations are not combined. Each date is
labelled as an exact (closed) endpoint or an uncertain (open) bound. Confirmed
findings, uncertainty, and processing failures use different labelled colors.
Excluded groups have one explanation; an Act 250 label comes from the saved scope
reason, not a separate extraction field. Evidence buttons open quotations in a
modal with frozen page text and original PDF links. Outcome explanations remain
visible; timelines, source documents, review notes and raw records are expandable.
The dashboard is read-only and listens on
`LISTEN_HOST` (all interfaces by default); viewing or refreshing it makes no model calls.

For the whole archive, explicitly plan with `--all` instead of `--group` and set
the run's spending cap before executing. Nothing in planning makes API calls.
Groups above 400,000 rendered source characters are preserved but marked too large;
they are never truncated. They require a separate route. `--root PATH` before the
subcommand selects a different run directory.

## Contract

- Scope: appeals of municipal decisions concerning new, replacement, or newly
  authorized housing, including ADUs, seasonal conversions, residential lots, and
  care apartments. Beds alone, ordinary alterations, and Act 250-only cases are out.
- One record per municipal appeal docket, with related proceedings linked. Who
  appealed, what permission was challenged, housing quantities, actual discretionary
  appeal basis and centrality, observed outcomes, restrictions and housing changes.
- Discretion is established from an actual appeal contention, never just a permit
  label. No exhaustive grounds analysis, conduct findings, or development feedback.
- Duration has two evidenced endpoints. Start is `exact` or `on_or_before`; end
  is `exact` or `on_or_after`. Two exact endpoints yield an exact day count;
  otherwise their difference is a minimum. Missing/invalid evidence, reversed
  intervals and uninformative zero-day minima produce unknown duration.
- Linked higher review, reopening and substantive municipal remand review count.
  Future judgments and retrospective mentions cannot masquerade as observed events.

Example, with citation objects omitted here:

```json
{"duration_bounds":{
  "start":{"date":"2024-07-25","relation":"on_or_before"},
  "end":{"date":"2024-10-16","relation":"exact"}},
 "duration":{"kind":"at_least","days":83}}
```

Use bounds, not the descriptive litigation status, for duration analysis. Do not
sum overlapping linked dockets, label an average of minima as actual duration,
or interpret court proceedings as proof of construction delay. Exact means exact
on the supplied record; these opinions are not guaranteed complete case histories.

## Stages and outputs

The steps and their prompts now live in `pipeline/legacy/` ([pipeline](PIPELINE.md)).
Plan froze the prompts and source hashes into `data/appeals/<name>/`. Source and prompt edits require a new run.
Triage and extraction share a conversation. The separately primed editor reviews
at most two retained groups together, bounded by source size. Explicit triage
exclusions finish immediately without another model call; uncertain scope continues
to extraction. Each successful review is followed immediately by a focused duration
question in the same conversation. The follow-up preserves the entire review
request as its exact prefix, appends the review answer and corrected target records,
and refers to sources already in that history. This enables provider prefix caching
without embedding another source copy in the follow-up message. Dates are subtracted
in Python. A flagged or invalid group does not block successful neighbors.

`--workers` controls concurrent first-pass groups and finishing batches (default
three). Each finishing worker performs review then duration for its own batch.
Completed groups appear immediately, without an archive-wide review barrier.
Terminal messages identify batch starts, failures and completion.

| Path within a run | Content |
|---|---|
| `manifest.json`, `prompts.json`, `sources/` | Frozen scope, settings, documents and exact inputs |
| `calls/` | Requests, responses, parsed output, usage, costs and reservations; no API key |
| `first_pass/`, `results/` | Drafts and integrated reviewed results, including failures |
| `export/records.jsonl` | Completed docket records with integrated duration bounds |
| `export/coverage.jsonl` | Every planned group, including exclusions, failures and oversized groups |
| `export/review.jsonl` | Warnings, unresolved issues and uncertain scope |
| `export/sources.jsonl`, `summary.json` | PDF mappings and run totals |

`scope_confirmed` and `review_required` are separate: complete extraction does not
imply confirmed scope or manual acceptance. Failed/incomplete drafts remain
inspectable on disk but are not exported as completed research records. No old
schema batches or experimental results are silently merged into a run.

## Resume and validation

Repeat the same run command to reuse completed identical calls without spending.
`--max-spend` is a cumulative cap for that run, including conservative reservations
for uncertain deliveries. Increase it to resume after a budget stop. Use
`--price-multiplier 2` for peak pricing; the default is non-peak 1×. Only one process
can operate on a run at a time. Inputs, prompts and batch membership are frozen.
For a run begun with the former six-group finishing structure, restore a backup
from before finishing before using this scheduler, or start a new run. There is no
migration path for old review/duration results; first-pass requests remain reusable.
Do not edit frozen prompts or sources.

Malformed JSON gets deterministic repair and at most one syntax-only model repair,
both logged; there is no repeated substantive self-review. Unknown network delivery
or other failed requests are never automatically resent. To retry failed calls:

```sh
make appeals-run APPEALS_ARGS='--name my-run --max-spend 1.00 --retry-failed'
# If a provider supports a larger output allowance:
make appeals-run APPEALS_ARGS='--name my-run --max-spend 1.00 --retry-failed --retry-max-output-tokens 131072'
```

Each failed call gets at most one retry per invocation. Successful calls are reused.
Completed group checkpoints are authoritative on resume: their records are neither
re-reviewed nor re-measured, even if their batch neighbor needs a review retry.
Successful first-pass checkpoints are reused without rewriting them. Review batches
are still packed from the entire frozen first pass, preserving request membership
and source prefixes. Only unfinished members are validated and targeted for duration.
Review validation operates on a copy; the original answer used in the follow-up
conversation remains unchanged.

Duration calls are named `duration-<batch-id>-<request-sha256>`. The hash includes
the complete request: review conversation, answer, corrected records and target
membership. Identical inputs reuse their call; changed inputs create a separately
logged and charged call. Earlier duration requests and responses remain intact and
included in spending totals. Existing completed results with older call names need
no migration. Unfinished groups receive calls using the new request identity.

Before retrying a saved extraction schema failure, the runner tries deterministic
field relocation and full validation. Root timeline, status, restriction and housing
change fields can move into a sole docket; misplaced discretion fields can move
into that docket's existing discretion object. Destinations must be absent, and
ambiguous or conflicting values are not overwritten. No evidence or classifications
are invented. Original responses remain unchanged; `*.original-parsed.json` and
`*.recovery.json` preserve the original parsed output, failed state and corrections.
Recovered calls retain their existing charge and make no new API request.

To raise an existing run's source eligibility cap and inspect its first pass before
review, use (with your chosen cumulative spending cap):

```sh
make appeals-run APPEALS_ARGS='--name full-run --max-chars 600000 --retry-failed --first-pass-only --max-spend 10.00'
```

`run --max-chars` may only increase the saved cap. Under the run lock it updates
eligibility from frozen source text and records the change in `manifest.json`;
sources, prompts and successful requests remain unchanged. This allows larger
groups to be attempted, subject to the provider's input limits. `--first-pass-only`
exports checkpoints and stops before review, preserving existing later-stage
results. Resume without that flag to start review once no first-pass failures
remain. The dashboard distinguishes validation, API and spending-cap failures,
including saved call metadata from older runs.

Prior attempts move into `calls/attempts/` and remain included in cost/token totals.
Output-limit failures record `finish_reason: length` and `error_type: output_limit`;
partial output is retained, never treated as completed JSON. HTTP, transport,
invalid-response and schema-validation errors are also saved. Retrying an uncertain
network delivery can incur a second provider charge; its original reservation stays
in the budget. Increasing the output allowance does not change successful calls.
Changed upstream review answers produce distinct duration request identities.
Other conflicts with frozen requests still stop rather than silently mixing versions.
Schema and quote checks do not
prove semantic correctness; consequential review flags still require attention.

Run `make appeals-test`; the broader suite is `python -m unittest discover -s tests`.
For a fresh finishing-only sanity check on saved first-pass data:

```sh
uv run --env-file .env python appeal_sanity.py --from-run release-check --max-spend 1 --workers 3
```

This creates a separate run, preserves the baseline, makes new review/duration calls,
and writes `sanity-report.json` with stage token usage, cache hits, reasoning tokens,
cost, wall time, corrections, evidence warnings and fixture duration comparisons.
The cap includes inherited first-pass charges. Fixture agreement is a regression
check, not proof of semantic correctness; inspect the saved corrections and bounds.
For the saved release batch, `python appeals.py check --name release-check`
compares scope and durations with the source-reviewed regression fixture without
making API calls.
The [release check](APPEALS-RELEASE-CHECK.md) documents the fresh model regression.
The [research summary](APPEALS-RESEARCH.md) preserves decisions and limitations.
The [cleanup record](APPEALS-DELETION-PLAN.md) lists the obsolete research artifacts
removed after authorization. Source data, shared dependencies and supported run
logs are preserved.
