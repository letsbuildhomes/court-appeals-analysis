# Cases flagged at review lose their records

- Status: open
- Opened: 2026-10-12
- Needed: before the final run

**What:** A case's scope can end up "uncertain" in two ways, and they're
handled differently:
- When the first pass (triage) is unsure whether a case is in scope, the case
  keeps its extracted records.
- When review flags the scope decision as doubtful, the case becomes uncertain
  with *no* records, and the review's corrected draft is thrown away.

September did this, and the reproduction keeps it. But `PIPELINE_CONTRACT.md`
says uncertain cases keep their records.

**Why it matters:** Researchers can't decide for themselves whether to
include flagged cases, because there's nothing to include. In September,
117 cases ended up uncertain.

**Recommendation:** A new version of the final assembly step that keeps the
reviewed draft records for flagged cases, tested as an experiment.
