## Step 2: conservative municipal housing appeal records

Read the included documents together. Return one record per municipal appeal
docket, not per document. Linked further review belongs in that appeal's history.
Separate municipal dockets stay separate even when they concern the same homes.
A docket gets its own record only when a supplied document addresses that appeal
directly, not when it merely recounts it as historical background.
Do not create a municipal record for an Act 250 or declaratory-judgment proceeding.

Extract only what the documents establish. This is a compact research record,
not a complete case analysis. Unknown is a useful answer; it is not a negative
finding. Do not infer opposition motives, construction delay, homes actually
built, or abandonment from the litigation or permit result.

Return this structure, replacing placeholders with facts. Empty lists mean no
established observation. Omit placeholder items. Dates can be YYYY-MM-DD,
YYYY-MM, YYYY, or null. Evidence entries are exact continuous quotations of at
most 25 words with their document and page; use multiple short entries when needed.

```json
{"schema_version":"v6","dockets":[{
  "docket":null,
  "related_dockets":[{"docket":null,"relationship":"","evidence":[]}],
  "project":{"description":null,"municipality":null,"proposed_units":null,"proposed_lots":null,"evidence":[]},
  "municipal_decision":{"description":null,"date":null,"outcome":"unclear","evidence":[]},
  "appellants":[{"name":"","relationship":"unclear","seeks":"unclear","representation":"unclear","evidence":[]}],
  "discretion":{"description":null,"evidence":[],"involved":"unclear","central_evidence":[],"central":"unclear"},
  "timeline":[{"kind":"filing","date":null,"court_or_body":"","dockets":[],"description":"","evidence":[]}],
  "observed_status":{"litigation":"unclear","permission":"unclear","description":null,"evidence":[]},
  "restrictions":[{"description":"","start":null,"end":null,"evidence":[]}],
  "housing_changes":[{"description":"","evidence":[]}]
}]}
```

**Identity and proposal.** Docket identifiers identify proceedings, not published
opinion citations. Use null if unavailable. related_dockets requires an evidenced
distinct proceeding, not conflicting spellings or another application. Describe
the proposal at stake, with proposed_units counting new, replacement, or additionally
authorized dwellings, excluding settled existing authorizations. Physical existence
does not itself establish legal authorization. proposed_lots counts new residential
building lots, excluding retained lots with existing housing and nonbuilding
remainders. A subdivision's total lot count is not its new residential lot count;
use null unless the new residential opportunities can be distinguished. Neither lot counts nor bedrooms
establish dwelling counts. If counts or their meaning are uncertain, use null.
Identify replacements as replacements, not net additions. Cite quantities directly.

**Municipal action and appellants.** municipal_decision describes the challenged
board/administrator action, with outcome approved, approved_with_conditions,
denied, other, or unclear. Its date is not the court filing date. Appellants are
those bringing that challenge or cross-appeal, not every participant. relationship:
developer (including an owner seeking housing permission), neighbor, organization,
other, unclear. seeks: overturn_approval, overturn_denial, change_conditions, other,
unclear. representation: represented, pro_se, unclear. Do not infer pro_se from an
absent lawyer's name. Support names, roles and classifications with evidence.

**Discretion: one finding, two linked facts.** First describe the actual reason for
challenging the municipal action. For involved=yes, evidence must establish both
(1) an actual contention in this appeal and (2) that its contested municipal test
requires evaluative judgment (for example, adequacy, compatibility, or a reasonable
relationship), rather than only a fixed numerical rule. The connection must be
visible in the cited passages, not supplied from knowledge of a permit's label.
A dispute can involve legal interpretation AND application of an evaluative test;
being called a legal question does not establish that it is nondiscretionary.
When the test or connection is unstated or ambiguous, use involved=unclear, keeping
the evidenced reason in description. A requested remedy's name is not its test.
Use no only when the described appeal basis sufficiently establishes exclusively
fixed rules, facts, legal/procedural rights, or judicial review standards.
An account of only some decided motions does not establish the absence of other
appeal contentions. If the remaining appeal basis is materially unestablished,
use unclear rather than classifying the whole appeal from the decided portion.

central=yes requires separate evidence that an established discretionary contention
was a principal component of the challenge; central=no requires evidence it was
peripheral. Otherwise central=unclear. Success or procedural dismissal establishes
neither involvement nor centrality. If involved=no, central=not_applicable; if
involved=unclear, central=unclear. Do not inventory or score every ground.

**Chronology and observed result.** Start timeline with the municipal appeal's court
filing (date null if unknown), then only material milestones across linked review.
kind: filing, judgment, further_appeal, reopening, remand, resolution. Each event's
date and description must refer to what happened then, not an earlier event quoted
in a later order. An order correcting an earlier decision without changing the
result is a judgment describing the correction, not a reopening or new resolution.
Keep the latest observed order; do not derive duration or an endpoint from list order.
Use resolution only for an explicit conclusion attributed to that event's own date.
Every non-null event date requires evidence of that event occurring then. A docket
identifier, a deadline, or a later document's filing date does not date the appeal's
initiation. A date on a procedural document dates that document, not the entire
case. Preserve uncertainty with date=null, even if a possible month can be inferred.
Describe each order's own action only: reporting a prior conclusion is not itself
concluding the appeal. Keep the filing item with date=null when initiation is undated.

litigation=ongoing includes substantive municipal review ordered on remand and
pending further court decisions/review; resolved requires an observed conclusion
without an observed pending adjudication; otherwise unclear. Compliance with an
already granted permit's conditions is not itself continuing adjudication.
Distinguish permit compliance from the court's unfinished work: if the court
still expects to enter a final judgment after receiving documents, that judgment
is pending. A merits approval alone does not establish the case's conclusion.
permission describes the proposal's latest observed authorization in this docket:
allowed, allowed_with_conditions, denied, or unclear. Describe companion outcomes
when needed to avoid mistaking a denied procedural route for a stopped project.
observed_status.description summarizes permission and any pending adjudication;
date attribution and which order concluded what belong only in timeline. Do not
retell the chronology in this status summary.
These are observed authorizations, not predictions or proof of construction.

**Consequences.** restrictions contains only explicit orders suspending/prohibiting
proceeding, with evidenced dates or null. Denial, remand, and ordinary approval
conditions alone are not stop orders. housing_changes describes explicit changes
or conditions imposed relative to the submitted proposal, with evidence; a grant
of the proposal itself is not a change. Do not turn existing-to-proposed dwelling
counts into an appeal-imposed change, or denial into a reduction to zero. Leave
quantitative before/after coding to review of these passages; there are no numeric
change fields. Preserve a clear imposed size/design reduction in the description.

Return research fields only; do not include development feedback, uncertainties, or suggestions.
