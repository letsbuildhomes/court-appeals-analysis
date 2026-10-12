# Research and design summary

> Historical record. The code this describes has been replaced and its commands no longer run; see [ARCHITECTURE.md](ARCHITECTURE.md) for the current system.

The report asks how municipal appeals slow, shrink or stop housing; whether the
appeals rely on discretionary municipal review; and who appeals. Developer appeals
of denials are a secondary question. The extraction must record evidence without
assuming the research hypothesis is true. Act 250-only matters remain excluded.

We began with broader, increasingly detailed extraction schemas and iterative
prompt revisions. Those improved individual cases but accumulated brittle rules,
especially around grounds analysis, docket attribution and finality. We narrowed
the record to the research questions: one evidence-backed discretionary finding,
appellant identity, housing quantities, observed consequences and litigation history.
Conduct findings and exploratory feedback were removed from the final contract.

We compared reasoning settings and other models, and tried source-first observation,
document-level assembly, specialist/adversarial agents, adjudication and review
loops. They did not consistently improve complete outputs. Detecting an error did
not guarantee its correction, and extra review sometimes removed sound findings.
Larger or different models alone did not solve that problem.

The useful change was a separately primed editor taught the research judgments
through worked examples, then given full documents and a draft. Bounded patches
fixed roles, dates, restrictions and overlooked discretionary contentions. On a
six-case transfer comparison, joint review corrected three targeted errors versus
two for individual review, at about 2.7¢ versus 10.7¢. It also lost one imposed
condition. The comparison is small and purposive, not an accuracy estimate.

Duration remained the weakest field. We replaced dependence on a “resolved” label
with two witnessed bounds: an appeal existed by a date and persisted through a
date, with exact endpoints only when established. A final duration experiment
covered six groups/nine docket records: one exact interval, seven useful minima,
one uninformative bound; manual review found no inflated interval in that sample.
The remaining zero bound was conservative incompleteness, not proof of no delay.

The selected design is therefore whole-group triage/extraction, primed batch
review, and a compact duration pass with deterministic arithmetic. Original
responses, quotations, uncertainty, costs and coverage remain inspectable.
Repeated general self-review is not included. Known exposure remains in linked
histories, centrality and completeness of imposed conditions; this is assisted
research extraction, not a guarantee of perfect case reconstruction.

Non-peak planning rates are $0.15/M uncached input, $0.003/M cached input and
$0.60/M output, including reasoning. The calibrated projection for 1,391 normally
eligible groups with review and duration is about 38.76M input + 13.91M output
tokens, $12.86 base; roughly $21 allows routing variation and 25% reserve. Four
oversized groups and human review are outside that estimate. Small purposive
review samples make this provisional. The preserved calibration snapshot is
`docs/appeals-cost-projection.json`; final run logs provide actual usage.

Historical experiments spent about $0.883 before the release cleanup. The release
regression is reported separately; production exports do not combine those earlier
versions or convert all experimental observations into independent sample counts.
