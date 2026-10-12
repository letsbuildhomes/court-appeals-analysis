# Publish the final results to the research server

- Status: open
- Opened: 2026-10-12
- Needed: after the final run

**What:** Researchers query the data through an MCP server that reads a
snapshot database. That snapshot is still built from an old trial run. The
cloud MCP container's instructions were updated, but the container hasn't
been rebuilt or redeployed since.

**Recommendation:** When the final results exist, build a snapshot from them
(`python -m court research build`), publish it and redeploy the container
(`python -m court research publish`; see `docs/MCP-CLOUD.md`), then run
`python -m court research smoke` against it.
