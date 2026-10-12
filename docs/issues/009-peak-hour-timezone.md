# Peak-hour pricing may use the wrong day

- Status: open
- Opened: 2026-10-12
- Needed: any time

**What:** DeepSeek doubles prices during weekday peak hours. The model catalog
(`pipelines/models.json`) defines both the hours and the weekdays in UTC. If
DeepSeek decides what counts as a weekday in Beijing time, then late Sunday
UTC (already Monday in Beijing) and late Friday UTC (already Saturday) are
priced wrong.

**Why it matters:** Only cost estimates and the spending cap's accounting,
not answers.

**Recommendation:** Check DeepSeek's pricing page. If weekdays are Beijing
time, it's a one-line change to the catalog.
