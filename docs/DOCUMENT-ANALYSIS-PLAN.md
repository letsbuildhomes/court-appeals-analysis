# Document analysis: scouting to full corpus

> Historical record. The code this describes has been replaced and its commands no longer run; see [ARCHITECTURE.md](ARCHITECTURE.md) for the current system.

Use the court PDFs to build **PDF-observed housing project histories**:
projects linked to cases, issues, events, and documents. Focus on municipal
appeals while retaining related Act 250 and later court activity. The opinion
library is not a complete docket or evidence that homes were built.

## Extract once, preserve the source

Aim for one long-context model request per PDF, using full selected text with
page markers. Extract docket numbers and their roles, court actions, dates,
appeal tracks, parties, project and housing facts, issues, events, outcomes,
and case relationships. Pair provisional enums with plain-language summaries
and source wording. Cite each factual claim to a page and supporting passage;
mark absent or uncertain facts. Save the raw response and input, prompt,
schema, and model versions. Track token use and handle oversized documents as
exceptions.

Ask the model for **additional useful observations** that the fields missed,
with page evidence and why they matter. It may suggest new fields or
categories; those suggestions inform later schema revisions.

## Learn in small batches

Start with about 25 PDFs varied by year, text source, document type, subject,
and length. Grow through fresh and repeat batches as the prompt evolves.
Review awkward categories, docket formats, false positives, omissions, and
model suggestions. There are no pre-labeled linked cases: discover candidate
links from extracted evidence, then check them against the cited PDF pages.
Keep a repeatable sample to detect regressions.

Then test whether claims assemble into cases and project episodes that can
answer questions about elapsed time, actors, locations, grounds, dismissal or
sanction indicators, stays, and changes to housing proposals. Keep conflicts
and missing endpoints visible.

## Roll out and monitor

Freeze a candidate prompt and schema, then process the archive in batches of
about 25 PDFs. Checked listing dates set processing order; case-history dates
come from the PDFs. After each batch, check extraction errors, `other` and
unclear labels, citations, links, and project summaries. Histories remain
provisional until the archive is processed. Retain every PDF and extraction
result for recoding or targeted reprocessing.

**Next build:** a versioned extraction runner, loose v0 schema and prompt, and
a compact scouting-batch report.
