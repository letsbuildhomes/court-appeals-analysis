# Research MCP

Freeze one explicitly selected run's finalized checkpoints under its shared run
lock, then query structured measurements with arbitrary read-only SQLite SQL.
The server uses the [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/)
(the optional `research-mcp` extra pins the v1 maintenance API below v2). It makes
no model calls, needs no pipeline credentials, and never silently refreshes its
snapshot. The initial implementation plan and client conversation checks are in
[MCP-ACCEPTANCE.md](MCP-ACCEPTANCE.md).
For plain HTTP, Docker and a coworker's CLI, see the
[quick HTTP experiment guide](MCP-CLOUD.md).

The snapshot contains no source documents, quotations, descriptions, review
issue prose, or fresh model classifications. Related-proceeding relationship text
and timeline court/body text are not controlled categories and are omitted.
Appellant names, docket IDs, and municipality names are retained as structured
identities. Source locations are omitted; hashes identify frozen inputs.

## Select and build locally

From this repository:

```sh
make research-setup
make research-list
make research-build MCP_RUN=final-smoke-20260929-190157
make research-describe MCP_RUN=final-smoke-20260929-190157
make research-test
```

`research-list` displays creation dates and checkpoint completion counts but does
not select a run. `final-smoke-20260929-190157` is the most recent completed local
run used for development; later sanity runs contain unfinished groups. Select a
run explicitly even after another run finishes. The Make targets use `--inexact`
to preserve other packages already installed in this project's environment.

A transferred run can be selected by directory without changing the pipeline:

```sh
.venv/bin/python research_mcp.py build \
  --run-dir /absolute/path/to/transferred-run \
  --output data/research/production-v1
```

Building requires that the selected run's `.lock` already exists and no writer
holds it. It reads `manifest.json` and saved `results`, `first_pass`, and `activity`
checkpoints, following the dashboard's newest-checkpoint/interruption rules.
It reads no exports, source text, PDFs, calls, or environment secrets. A group
contributes docket facts only if its selected checkpoint is complete; unfinished
and excluded groups remain visible in coverage. This allows snapshots of a
partially completed run without presenting drafts as finalized observations.

The build validates v6 structured categories, identities, quantities, dates,
unique docket membership, and duration arithmetic. Existing evidence validation
and review flags are preserved; the build does not reassess quotations or the
substantive truth of the extraction. Inputs are hashed and checked for stability
before publication. The SQLite file and provenance manifest are published
atomically to a new directory; existing snapshots cannot be overwritten.
The database hash and expected schema are verified at startup, and the server pins an in-memory
image. Build a new directory to adopt subsequent pipeline changes.

## Claude Desktop

Generate an exact, mergeable configuration snippet with absolute paths:

```sh
.venv/bin/python research_mcp.py client-config \
  --snapshot data/research/final-smoke-20260929-190157
```

Merge the returned `housing-appeals` entry into `mcpServers` in Claude Desktop's
configuration (Settings → Developer → Edit Config), keeping existing entries.
Restart Claude Desktop. This command prints configuration; it does not edit the
user's client settings. The generated command directly uses this project's
Python, so starting Claude does not resolve dependencies or require a terminal
working directory. The local setup follows the
[official MCP client guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

The two tools are `describe_dataset` and `query_sql`. Ask Claude to call the
dictionary first, define the population/grain, and retain missing/uncertain
observations. For example: “Using the research MCP, among confirmed appeals with
only pro se appellants, count discretionary involvement as yes/no/unclear and
show the SQL and snapshot ID.”

Stdio is the default. Optional loopback HTTP:

```sh
.venv/bin/python research_mcp.py serve \
  --snapshot data/research/final-smoke-20260929-190157 \
  --transport streamable-http --port 8770
```

The endpoint is `http://127.0.0.1:8770/mcp`. External binding uses `--host 0.0.0.0`
and exact `--allowed-host` entries described in [MCP-CLOUD.md](MCP-CLOUD.md).
Use a client supporting
Streamable HTTP. Automated tests verify real MCP initialization, discovery,
structured results, denied calls and recovery through both transports. They do
not establish that Claude or another model passes the conversation checks.

## Query and counting semantics

`describe_dataset` provides every column's definition, available categories,
joins, SQL functions, snapshot identity, scope counts and limits. Complete
per-file/source hashes remain in the snapshot's `manifest.json`; the tool returns
a compact provenance header so discovery stays bounded as the dataset grows.

| Object | Grain and population |
| --- | --- |
| `appeals` | Default view: one finalized confirmed municipal housing appeal per `(group_id,docket)` |
| `uncertain_appeals` | Finalized records with no explicit negative scope finding but unconfirmed municipal/housing scope |
| `all_appeals` | All finalized docket records, including explicit scope exclusions; inspect `scope_category` |
| `coverage` | One planned source group, including excluded/incomplete/failed processing |
| `appellants`, `related_dockets`, `timeline_events`, `restrictions` | One listed child observation per appeal and ordinal |
| `timeline_dockets` | One docket link per event; join with appeal ID and event ordinal |
| `review_flags` | One review flag per group and ordinal; null docket means group-wide |

Route exclusions (`out_of_scope`/`no_members`) override positive scope values.
Pipeline failure is a coverage status, never an appeal outcome. Review-flagged
records remain included, matching the dashboard; `review_required` enables an
explicit filter. Warning and review flag counts are preserved without their prose.

Representation is calculated from **all** listed appellants: all known pro se
gives `pro_se`; all known represented gives `represented`; both gives `mixed`;
any unknown or an empty/missing list gives `unclear`. Filtering individual pro se
appellants first would incorrectly include mixed and uncertain cases.

```sql
SELECT discretion_involved, count(*) AS appeals
FROM appeals
WHERE representation = 'pro_se'
GROUP BY discretion_involved;

-- Count appeals with any neighbor appellant without multiplying appeals.
SELECT a.permission, count(*) AS appeals
FROM appeals a
WHERE EXISTS (
  SELECT 1 FROM appellants p
  WHERE p.appeal_id = a.appeal_id AND p.relationship = 'neighbor'
)
GROUP BY a.permission;

-- Keep exact and minimum intervals distinct, with unknowns visible.
SELECT duration_kind, count(*) AS appeals, avg(duration_days) AS mean_supported_days
FROM appeals
GROUP BY duration_kind;
```

Run SQL directly without an MCP client:

```sh
.venv/bin/python research_mcp.py query \
  --snapshot data/research/final-smoke-20260929-190157 \
  --sql 'SELECT discretion_involved, count(*) FROM appeals WHERE representation=? GROUP BY discretion_involved' \
  --parameters '["pro_se"]'
```

Ordinary SELECT, joins, CTEs, grouping, window functions and bound parameters are
supported. Results contain column/row arrays (duplicate aliases remain intact),
the actual SQL/parameters, snapshot/run identity, returned count, truncation and
interpretation limits. Page large lists using stable `ORDER BY`/`LIMIT`/`OFFSET`;
a truncated answer is not exhaustive. `max_rows` defaults to 200 and caps at 1000.

Unknown scalar observations are null or `unclear` as defined by the schema.
List states distinguish `documented`, `none_documented` (an empty extraction list)
and `unknown` (unavailable list); no established observation is not proof of
absence. Partial timeline/municipal/restriction dates preserve `YYYY`/`YYYY-MM`;
do not treat these as exact dates. Duration dates are full dates with independent
exact/bound relations. Durations are computed from validated bounds, retain
`exact`/`at_least`/`unknown`, and suppress reversed or zero-day minimum intervals.
An exact zero-day interval remains valid. No endpoint is extended to today.

Permission is observed authorization, not appellant success. Proposed units/lots
may repeat across related dockets, are not distinct built homes, and may include
replacements. Housing changes have only documented presence/count, since the
extractor did not code quantitative before/after impacts. Null restriction end
does not prove an ongoing stop order. These measurements cannot establish
motivation, construction delay, abandonment, prevented homes, or causation.

## SQL boundary

Each query gets a separate connection to the pinned memory image with
`query_only`, disabled extension loading and double-quoted string fallback, an
authorizer allowing only selected table columns/SELECT/approved functions, and
SQLite size/instruction/time limits. Schema metadata, PRAGMAs, attachments,
transactions, writes, file functions, extensions and unbounded blob functions
are denied. SQL is limited to 16,000 characters, parameter JSON to 16,000 bytes,
result JSON to about 256 KB, and execution to two seconds/five million VM steps.
The allowed function list is discoverable rather than inferred from SQLite.
Discovery reports only allowed functions available in the serving SQLite runtime.

The server guarantees that its dataset and tools expose only the selected
structured facts. It cannot control a chatbot's independent tools or prevent
unsupported interpretations; use the [conversation acceptance checks](MCP-ACCEPTANCE.md)
to assess those behaviors separately.
