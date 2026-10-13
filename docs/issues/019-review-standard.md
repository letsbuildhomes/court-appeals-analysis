# Extract whether each appeal was heard on the record or de novo

- Status: open
- Opened: 2026-10-13
- Needed: before the final run

**What:** The user wants to know, for each docket, whether the court heard the
appeal on the record of the municipal proceeding or de novo. Vermont towns
that adopt on-the-record procedures (24 V.S.A. § 4471) send appeals to the
court on the board's record; elsewhere the court hears them anew. Decisions
often say which ("This appeal is taken as an on-the-record appeal").

**Why it matters:** The research contract is about to be fixed by the
reference set and a final run. Adding a question later means relabeling and
rerunning.

**Progress:**
- 2026-10-13: The results contract has it as `appeal-result-2`'s
  `appeal_review` field (`on_the_record`, `de_novo` or `unknown`, with
  evidence), and the lab compares it. Version 1 results stay valid and aren't
  counted as missing it. The reference labels answer it from the start.

**Recommendation:** Teach the pipeline to answer it as an experiment against
the reference set: a draft prompt variant that asks it and an assembly
variant that writes version 2 results. The research snapshot needs a column
for it too.
