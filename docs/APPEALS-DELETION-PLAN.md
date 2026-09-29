# Completed research cleanup

**Deleted on September 29, 2026, following the user's authorization.**
All 1,496 candidates matched their recorded size and SHA-256 before deletion.
The dependency check found no retained Python imports of the deleted modules.
The manifest remains as the exact historical deletion inventory; it is not a
pending list to regenerate.

**1,496 files, 31.49 MiB** were removed, along with 92 empty research directories.
Every deleted path, original size, SHA-256 and reason is in
[appeals-deletion-manifest.json](appeals-deletion-manifest.json).

| Category | Files | MiB |
|---|---:|---:|
| Retired experiment runner | 9 | 0.04 |
| Superseded notes; conclusions preserved in APPEALS-RESEARCH.md | 9 | 0.08 |
| Superseded research entry point or scratch file | 7 | 0.05 |
| Experiment logs, audits, samples and research utilities | 1446 | 31.24 |
| Superseded experiment prompt; selected contract preserved in prompts/appeals | 15 | 0.05 |
| Tests only for retired research entry points | 10 | 0.03 |

## Preserved

- Original PDFs, archive/text/group/scout databases, all production data and new
  `data/appeals/` run logs, frozen prompts/sources and final exports.
- The selected six prompts under `prompts/appeals/`, supported runner/visualizer,
  evidence validators, JSON repair, direct API client, and their active tests.
- `APPEALS.md`, `APPEALS-RESEARCH.md`, `APPEALS-RELEASE-CHECK.md`, and the cost
  calibration snapshot preserve the operating guide, research decisions and results.
- Shared older modules `group_scout.py`, `scout.py`, `claims_server.py` and
  `review_server.py` still provide validation, source access or PDF serving. Their
  compatibility prompts/tests and `notebooks/refinement/verify_group.py` remain;
  these are software dependencies, not deletion-ready logs.
- Upstream ingestion/text documentation and tools are outside this cleanup.

The retired experimental scripts are no longer available; the supported export
is `appeals.py export`. No production path imports a removed runner or reads
the deleted research logs. The release and final small-batch runs, including
their assessments, remain under `data/appeals/`. A hash check verified 309 retained
code, prompt, test, dashboard and run files were unchanged by deletion.

Only Git entries for the listed deletions were updated; all 96 unrelated staged
entries were preserved exactly. After deletion, the remaining suite completed
94 tests successfully (one skipped), the saved release regression passed with
no differences, and the dashboard could still load the archive and both recent
run datasets. Verification made no API calls.
