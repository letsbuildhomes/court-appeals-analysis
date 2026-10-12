# Research MCP

Research tools read one results map, frozen into a **snapshot**: a read-only
SQLite database of structured facts plus a manifest of its provenance. The
MCP server answers bounded, read-only SQL over one snapshot with the
[official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/v1/). It makes
no model calls, needs no pipeline credentials and reads nothing but the
snapshot. For plain HTTP, Docker and a coworker's CLI, see
[MCP-CLOUD.md](MCP-CLOUD.md).

A snapshot holds no source text, quotations, descriptions, reasons or other
prose: only categories, dates, counts and identities (docket numbers,
municipality and appellant names).

## Build and query

```sh
make research-setup
python -m court research build --results september-results --cases september-cases \
  --output data/research/september
python -m court research describe --snapshot data/research/september
python -m court research query --snapshot data/research/september \
  --sql 'SELECT disposition, count(*) FROM cases GROUP BY 1'
```

`--results` is any results dataset, by ID or name, such as a run's output;
`--cases` is the case set it came from, when the results' producer did not
record it. A snapshot's ID is the hash of its provenance (results, case set,
pipeline and run) and its database; building never overwrites a directory.

## Claude Desktop

Add a `housing-appeals` entry under `mcpServers` in Claude Desktop's
configuration (Settings → Developer → Edit Config), with this project's
Python as the command:

```json
{"command": "/absolute/path/to/repo/.venv/bin/python",
 "args": ["-m", "court", "research", "serve", "--snapshot", "/absolute/path/to/repo/data/research/september"],
 "cwd": "/absolute/path/to/repo"}
```

The tools are `describe_dataset` and `query_sql`. Ask Claude to read the
dictionary first, state the population and grain, and keep unknowns
visible. Stdio is the default; `--transport streamable-http --port 8770`
serves `http://127.0.0.1:8770/mcp`, and external binding needs `--host 0.0.0.0`
and exact `--allowed-host` entries ([MCP-CLOUD.md](MCP-CLOUD.md)).

## Tables and counting

| Object | Grain and population |
| --- | --- |
| `cases` | One case per row: processing status (`complete` or `failed` with its kind), disposition and scope answers |
| `appeals` | One record per `(case_id, docket)` for in-scope cases |
| `uncertain_appeals` | Records of cases whose scope is uncertain |
| `all_appeals` | Every record; inspect `disposition` |
| `appellants`, `related_dockets`, `timeline_events`, `restrictions` | One listed observation per appeal and ordinal |
| `timeline_dockets` | One docket link per event; join on appeal ID and event ordinal |

- A failed case has no disposition and no records; failure is processing
  status, never an appeal outcome.
- `unknown` is a value in category columns; text, date and count columns use
  null for unknown.
- An empty list means nothing was established, not that nothing happened.
- `at_least` durations are minimums, never complete durations.
- Permission is the latest observed authorization, not whether the appeal
  succeeded.
- Child tables multiply rows: filter with `EXISTS` or aggregate per
  `appeal_id` before counting.

## Bounds

Only `SELECT`, `WITH`, window functions and an allowed function set run, on
the snapshot's own tables. Queries are limited to 16,000 characters, 1,000
rows, 256 KB of results and two seconds; truncation is reported. The server
verifies the database hash and schema at start and serves an in-memory copy.
