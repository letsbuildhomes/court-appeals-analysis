# Review checks two cases per request

- Status: open
- Opened: 2026-10-12
- Needed: any time (deferred by choice)

**What:** The review step, where a second model pass checks each drafted case
against the decision text, sends two cases together in one request to save
money. Pairs are made by sorting the cases in the run and taking them two at a
time. Change which cases are in a run, or remove one, and many cases get new
partners.

**Why it matters:** A saved model answer is reused only for an identical
request, and a case with a new partner is a new request. So any run with
different cases pays for new reviews of cases where nothing changed, and those
answers can differ by chance or because the partner influenced them. In an
experiment, those changes look like effects of the change being tested. For
example, merging two drafted cases in the bracket caption fix re-pairs about
120 of the 442 reviewed cases.

**Options:**
1. A new review version that checks one case per request. Prompt text that
   repeats is billed at about 2% of the normal input price, so the extra cost
   should be small.
2. Keep pairs, but fix them once for the reference set and always run whole pairs.
3. Accept the extra cost and noise.

**Recommendation:** Option 1, adopted through an experiment, when it's
convenient. Until then, read experiment results knowing that re-paired cases
can change for reasons unrelated to the change being tested.

## Progress

- 2026-10-13: One-docket grouping ([015](015-grouping-quality.md)) adds about
  340 case IDs. Pairs are formed in case order, so nearly every pair after the
  first new case changes, and most reviews will be sent again whether pairs
  stay or not. Pairs also double the damage of a review that runs out of
  output ([017](017-review-output-limit.md)): the failed call breaks both cases.
