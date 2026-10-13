# Paths that have only run against fakes

- Status: open
- Opened: 2026-10-12
- Needed: before the first paid experiment

**What:** Two parts of the rebuilt system have only been tested with fake
stand-ins:
- downloading from the court website live
- sending real, paid model calls with credentials, including the peak-hour
  price charging

**Why it matters:** A problem there could waste money or leave a run half done.

**Recommendation:** Make the first real run of each small and capped (about
$1 for model calls), and watch it in the web app.

## Progress

- 2026-10-12: Paid model calls work. The bracket-columns probe sent 25 real
  calls under a $2 cap and was charged $0.30; the web app showed it live.
  One review call ran out of output room twice, which the run reported as a
  failed delivery rather than a wrong answer. Live downloading from the court
  website is still untested.
