# Old data directories

- Status: open
- Opened: 2026-10-12
- Needed: after the final results are published

**What:** `data/` still holds data in the old formats: `runs`, `pipelines`,
`experiments`, `appeals`, the old research snapshots and several DuckDB
files. Nothing reads them except the one-time September import.

**Recommendation:** Leave them alone, read-only, until the final results are
published, then archive them.
