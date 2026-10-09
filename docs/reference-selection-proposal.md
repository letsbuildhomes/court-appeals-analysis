# Reference selection proposal — not an approved sample

This document proposes future selection and adjudication. It selects no cases,
creates no production labels, and authorizes no model calls. The shipped demo
fixtures are software tests with invented facts, not evidence of extraction
quality or a proposed alternative extraction strategy.

## Unit, split, and sampling

Use the frozen source group as the evaluation unit. Before sampling, establish
related-case clusters using available docket relationships, shared proceedings,
documents, and known appeals or remands. Assign whole clusters to development or
held-out groups; never split related records or source documents across them.
Freeze source hashes and split membership before tuning. Resolve newly discovered
cross-split relationships by quarantining the cluster and versioning the split,
not by silently moving a difficult case into development.

Maintain two separately reported samples:

- A representative probability sample of the declared eligible population,
  with its sampling frame, inclusion probabilities, exclusions, and uncertainty
  recorded. Population claims require this design and appropriate weighting.
- An enriched challenge sample covering uncommon routes, uncertain scope,
  missing identifiers, multiple dockets, complex duration evidence, and failures.
  Use it to discover errors; do not interpret its unweighted accuracy as a
  population estimate or pool it invisibly with the representative sample.

Decide sample sizes after a small development pilot estimates prevalence,
review time, and disagreement. Keep held-out content and labels inaccessible to
strategy tuning. Repeated held-out comparisons require a recorded evaluation
policy to limit adaptation to the held-out answers.

## Adjudication and release

Reviewers use frozen source text and a written field rubric. Capture each
expectation's evidence locator or quotation, rationale, comparison rule, and
label version. A field's missing label means unreviewed, not a negative answer.
An adjudicated null or `unknown` value can instead be the rubric's explicit
answer for insufficient source evidence. Pending, disputed, and unknown *label
states* are never scored. Free-form prose remains diagnostic unless a future
reviewed semantic rubric supplies structured expectations.

Have independent reviewers label an initial overlap and difficult cases, blinded
to candidate output where practical. Record disagreement, adjudicate explicitly,
and leave unresolved fields pending or disputed. Declare the expected record set
complete only after reviewing all records; otherwise additional records remain
unscored diagnostics. Ambiguous record identity requires adjudication, not
positional matching. Release a versioned reference with source hashes and an
audit trail; preserve prior releases when correcting labels.

Compare baseline and candidate to the exact same reference release and frozen
source. Publish paired field denominators and coverage, record omissions and
additions, identity ambiguity, source incompatibility, and execution failures.
Do not convert unreviewed fields or failed execution into invented accuracy.

## Cost estimation before authorization

Measure pilot reviewer minutes per case, overlap and adjudication rates, source
length distributions, and any proposed model's measured input/output token
usage. Estimate human time as first review plus overlap plus adjudication and
administration; estimate model cost from current verified rates and measured
usage, including retries and a declared contingency. Report low/base/high
assumptions and separate inherited charges, projected new charges, and observed
new spending. Establish a spend cap and stopping rule before production labeling.
This proposal contains no price promises or selected production sample.
