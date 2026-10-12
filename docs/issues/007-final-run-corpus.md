# Which decisions the final run covers

- Status: open
- Opened: 2026-10-12
- Needed: before the final run

**What:** The pipeline works from the court website's listing as captured on
September 22, 2026. Decisions posted since aren't included.

**Options:**
1. Keep the September 22 snapshot.
2. Download a fresh listing first. New decisions become a new documents
   dataset, and grouping and extraction make new calls for the new cases.

**Why it matters:** It decides how current the published data is. A fresh
crawl also exercises live downloading, which has only run against fakes
([011](011-first-real-runs.md)).

**Recommendation:** Decide when the final run is near.
