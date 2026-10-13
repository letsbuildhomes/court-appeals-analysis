# The lab's measures misread grouping changes

- Status: done
- Opened: 2026-10-13
- Needed: before experiments

**What:** The first real experiment, `bracket-columns`, reported 7 fewer
docket records and 82 fewer research facts, and a higher share of facts
answered. Read by hand, it removed 2 duplicate records (the point of the
change), lost 1 to a failed call and 4 to one case's scope flag, and changed
answer completeness not at all. The measures compared records within each
case, so a record that moved to another case counted as lost, and a docket
held by two cases counted twice. Completeness was a share of whatever each
side processed, so failed cases raised it. Ten of the 13 "changed" cases had
the same documents, with only docket labels in their text changed, and their
verdicts were chance but looked like effects.

**Why it matters:** Grouping experiments move records between cases, so
every one would be misread.

## Outcome

Comparisons match records by docket across all compared cases: moved records
are compared fact by fact, duplicates are counted, and lost dockets are
counted once each with a reason (the case failed, was absent, or its
disposition or extraction changed). Completeness is measured on the records
both sides have. A changed case with the same documents is marked "same
documents", and a finished case whose input didn't change in substance is
not a regression. Recomputed from the cache, the bracket-columns probe now
reads 13 → 8 dockets with records, 2 duplicates removed, 5 dockets lost (4 to
a scope change, 1 to a failed call) and 113 → 115 of 136 facts answered on
shared records.
