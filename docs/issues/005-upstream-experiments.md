# The lab can't cleanly test changes to grouping or text

- Status: done
- Opened: 2026-10-12
- Needed: before the first grouping or text experiment

**What:** An experiment round runs on a list of keys, and those same keys are
used to compare results. That works for extraction changes, where case IDs go
in and the same case IDs come out. It doesn't work for a change earlier in the
pipeline:
- Grouping has to see every document; grouping a subset gives different cases.
- A grouping change can merge or split cases, so case IDs change.
- Only the cases whose documents or text changed need new extraction, but
  nothing in the lab finds them automatically.

**Why it matters:** Every grouping or text improvement hits this, starting
with the bracket caption fix ([004](004-bracket-captions.md)).

**Recommendation:** A general round type for upstream changes:
1. Run the whole corpus through the earlier stages. Text and grouping make no
   model calls, so this is free.
2. Use the case-set diff to find the cases that changed.
3. Run extraction for all cases. Unchanged requests come from the cache, so
   only changed cases are paid, plus any cases re-paired at review
   ([002](002-review-batching.md)).
4. Report changed cases separately from cases that changed only by chance or
   re-pairing, and report merges and splits as such rather than as lost or
   new cases.

Running the whole corpus also leaves a full set of results for the new
pipeline, so the next experiment can compare against it.

## Outcome

Done on 2026-10-12. An experiment's `--focus` names the position its keys
apply to; every step before it runs on everything. `--keys changed` chooses
the cases whose input at the focus changed, plus cases merged away or removed,
and `--confirm all` runs every case. Comparisons mark each case's input as
changed, removed, added or the same, and records that move with merged cases
need judgment rather than counting as losses. The whole pipeline runs from
the caches in under a minute. See "Changes before extraction" in
`docs/LAB.md`; the first use is the `bracket-columns` experiment
([004](004-bracket-captions.md)).

Unlike the recommendation above, a probe runs extraction only for the
changed cases, so it avoids re-pairing other cases at review. A confirm round
over every case still re-pairs them ([002](002-review-batching.md)).
