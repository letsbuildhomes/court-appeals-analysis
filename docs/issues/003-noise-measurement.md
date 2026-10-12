# Measure how much answers vary by chance

- Status: open
- Opened: 2026-10-12
- Needed: before experiments that change extraction

**What:** Model answers vary a little from one run to the next even when
nothing changes. DeepSeek's thinking mode ignores temperature, so we can't
switch that off. An "A/A test" runs the unchanged production pipeline again
on a set of cases and counts how many answers change by chance alone.

**Why it matters:** Without that number, a change that moves 3 answers out of
50 might be a real effect or might be noise.

**Notes:** Rerunning production unchanged returns the saved answers for free,
so an A/A test needs a way to request genuinely new answers. That option
probably doesn't exist yet: the call store keys calls by a sample number,
which could be used. The cases re-paired at review by an upstream change
([002](002-review-batching.md)) are a free, rough A/A measurement.

**Recommendation:** Run it on the reference set once that exists
([001](001-reference-set.md)).
