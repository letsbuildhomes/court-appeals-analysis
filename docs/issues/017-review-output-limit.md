# Review sometimes reasons until it runs out of output

- Status: open
- Opened: 2026-10-13
- Needed: before the final run

**What:** Review answers use 37,000 output tokens at the median and 60,000 at
the 90th percentile, almost all of it reasoning, against a 65,536 limit. In
the legacy run about 40 of 277 review attempts hit the limit; resending at the
same limit worked for all but 3. Input size barely matters (correlation
0.22). The calls that fail loop: the `bracket-columns` probe's failed call
spent its whole allowance reasoning, ending in thousands of "Hmm", on whether
a group mixing housing and nonhousing matters was in scope. Because review
checks two cases per call, one such failure breaks two cases.

**Why it matters:** Every large run will have some, and each costs a resend
and holds up its cases. A 131,072 limit would also run into the 650-second
HTTP timeout (about 200 tokens a second).

**Options:** one case per review call ([002](002-review-batching.md)); groups
that hold one appeal, which removes the mixed-scope dilemma
([015](015-grouping-quality.md)); a higher limit with a longer timeout for
resends; lower reasoning effort for review, tested as an experiment.

**Recommendation:** See how often it happens with one-docket cases, then
decide. Resend failures with `--resend-failed`, which now resends once per
execution, and `--resend-max-tokens` up to about 100,000.
