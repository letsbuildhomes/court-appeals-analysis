# Docket numbers missed in captions with bracket columns

- Status: open
- Opened: 2026-10-12
- Needed: before the reference set is labeled

**What:** Older decisions, mostly from 1996 to 2003, lay out the caption with the
parties on the left and the docket number on the right, separated by a column
of `}` characters. The extracted text reads:

```
Appeals of Wesco, Inc      } Docket Nos. 209-12-97
                           } Vtec, 175-10-01 ...
```

The grouping rule expects `209-12-97 Vtec` with only spaces in between, so the
`}` breaks the match and the docket is missed.

**Why it matters:** Docket numbers decide which documents form one case. A
missed docket can leave a case split in two or fail to join related appeals.

**Findings (2026-10-12):** A narrow rule that allows bracket columns and line
breaks between the number and "Vtec", and changes nothing else, was tested on
the September documents. It changes 14 of 2,560 documents. Every newly found
docket matches the document's own court listing or is plainly in the caption,
and no document loses a docket. 1,389 cases keep the same documents; three
pairs merge, all correctly:
- `g-31-2-03-vtec` joins `g-100-5-02-vtec`: consolidated appeals whose caption lists both.
- `g-283-22-01-vtec` joins `g-283-12-00-vtec`: the listing's docket has an impossible month (22).
- `g-pdf-1d6a247e32ec`, which had no docket, joins `g-284-12-02-vtec`.

**Recommendation:** Make it a new version of the grouping step and adopt it
through the lab. It's a good first experiment, but the lab needs
[005](005-upstream-experiments.md) to run it cleanly.

## Progress

- 2026-10-12: The new grouping version is `cases/group:caption-dockets-bracket-columns`,
  tested by the experiment `bracket-columns`. Its plan picks 13 changed cases
  plus the 3 merged away. The probe needs real model calls for those 13,
  probably about $1; a $2 cap leaves room.
- 2026-10-12: The probe ran on the 13 changed cases and the 3 merged away, for
  $0.30. 2 improved, 2 worsened, 2 need judgment, 8 unchanged. Two cases,
  `g-38-2-00-vtec` and `g-58-4-99-vtec`, broke: review checks them together
  in one call, and its answer ran out of room (the output limit) twice, the
  second time when resent. They need a larger output limit
  (`--resend-max-tokens`) before the verdicts are complete. The confirm
  round's plan picks every case; 67 need new calls, estimated at up to $1.83
  for the first calls.
