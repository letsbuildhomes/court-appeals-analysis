# Two-group finishing sanity check — September 30, 2026

The new scheduler reviews at most two retained groups per batch, with three
concurrent workers. Each worker asks the duration question immediately after its
review, preserving the exact review request prefix. Explicit exclusions complete
without a second scope call. Flagged or invalid members are omitted from duration
targets without blocking their neighbors.

The live check used a fresh copy of the eight-group `release-check` first pass.
The original run was not modified. The copied prompts, sources and drafts were
unchanged; only finishing requests were new. High reasoning and the 65,536-token
output allowance were retained. The cumulative spending cap was $1, including
copied first-pass charges. No failure retries were made.

Saved artifacts: `data/appeals/sanity-two-group-live-20260930/`, including
`sanity-report.json`, requests, provider responses, patches, results and exports.
Reproduce with `appeal_sanity.py --from-run release-check --name NEW-NAME` and the
configured API environment. A separate earlier sandbox attempt failed DNS before
delivery and incurred no new model charge.

| Measure | Saved baseline finishing | New finishing |
|---|---:|---:|
| Review calls | 4, including 2 exclusion reviews | 3 retained-pair reviews |
| Duration calls | 2 | 3 follow-ups |
| Review input / cached tokens | 100,461 / 18,560 | 88,169 / 10,752 |
| Duration input / cached tokens | 73,195 / 18,176 | 108,238 / 87,936 |
| Duration cached fraction | 24.8% | 81.2% |
| Review reasoning tokens | 84,116 | 157,455 |
| Duration reasoning tokens | 22,792 | 16,719 |
| Finishing charge | $0.088374 | $0.123334 |
| Request elapsed time, summed | 469.7 seconds | 724.9 seconds |
| New scheduler measured wall time | — | 265.0 seconds |

The baseline was a historical run, not a concurrent controlled experiment. Its
summed request times approximate the former sequential schedule, but are not a
measured baseline wall time. The new run also produced one fewer docket record.
The observed cache improvement is clear; an overall spending reduction is not.
Review reasoning increased enough to outweigh the duration input savings.

## Output quality

- Seven of eight groups completed: five retained groups and two exclusions.
- All three review requests returned complete model responses. None exhausted the
  output allowance, although two used 53,909 and 60,181 output tokens.
- Eight produced docket durations exactly matched the source-reviewed fixture.
- `g-106-10-18-vtec` failed review validation and produced no duration, so the ninth
  expected duration is explicitly reported as missing rather than counted as a pass.
- Its batch neighbor `g-75-4-06-vtec` completed, demonstrating failure isolation.
- The rejected patch attempted to replace a source quotation containing "would be
  voided" with "would no longer be of any effect." The proposed quotation is not
  present on the cited frozen page. The validator correctly rejected it; this is
  not a punctuation or citation-location repair opportunity.
- Existing unmatched-quotation and corrected-page warnings remain visible for
  other groups. Fixture agreement does not certify every research field.

The scheduler and cache-continuation behavior are verified, but this small run is
not a clean quality pass or evidence that two-group high-reasoning review is cheaper.
Review reasoning remains the main cost and output-limit concern. Any subsequent
reasoning/prompt adjustment should be evaluated on the same fixture and preserve
strict change-evidence validation.

The offline suite ran 114 tests successfully, with one skipped. New tests cover
concurrent batches, source-prefix preservation, exclusion completion without calls,
and successful duration processing beside a flagged or invalid review.
