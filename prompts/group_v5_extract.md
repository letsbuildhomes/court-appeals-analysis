## Step 2: municipal housing appeal research records

Read the included documents together. Extract the compact record below for each
municipal appeal docket in this matter. Include later review of that municipal
appeal as linked history; do not create a separate research record for its Supreme
Court docket or for an Act 250 appeal. Use only documents whose membership is yes
or unclear. If the docket number is unavailable, use null rather than inventing it.
Docket identifiers identify proceedings; published opinion citations are not
docket identifiers. A related proceeding may have docket=null if its identifier
is unknown; its relationship and evidence still describe the observed connection.
related_dockets requires a distinct connected proceeding. Conflicting identifiers
for the same proceeding are an identity uncertainty, not an established related case.
An earlier permit application is not a court docket; a cross-appeal within this
docket belongs among appellants, not related_dockets.

Return one JSON object with this complete structure. Repeat the docket record
only for distinct municipal appeal dockets; replace placeholders with source facts:
```
{"schema_version":"v5","dockets":[{
  "docket": null,
  "related_dockets": [{"docket":"identifier","relationship":"how related","evidence":[]}],
  "project": {"description":null,"municipality":null,"proposed_units":null,"proposed_lots":null,"evidence":[]},
  "municipal_decision": {"description":"decision appealed","date":null,"outcome":"unclear","evidence":[]},
  "appellants": [{"name":"name","relationship":"unclear","seeks":"unclear","representation":"unclear","evidence":[]}],
  "discretion": {"description":null,"evidence":[],"involved":"unclear","central_evidence":[],"central":"unclear"},
  "timeline": [{"kind":"filing","date":null,"court_or_body":"body","dockets":[],"description":"milestone","evidence":[]}],
  "observed_status": {"litigation":"unclear","permission":"unclear","description":"latest observed position","evidence":[]},
  "restrictions": [{"description":"restriction","start":null,"end":null,"evidence":[]}],
  "housing_changes": [{"description":"ordered change","units_before":null,"units_after":null,"lots_before":null,"lots_after":null,"evidence":[]}]
}],"scouting":{"uncertainties":[],"suggestions":[]}}
```

## Evidence and unknowns

Each evidence entry is {doc,page,quote}: an exact continuous passage of at most
25 words on that page. Supply enough passages to support each substantive claim,
including quantities and classifications, not just a shared name. Unknown facts
are null or unclear, not inferred from silence. Use empty arrays when no item is
established. Keep descriptions concise, normally one sentence. A document's
caption or posting date is not evidence of every event's date.

## Proposal, decision, and appellants

Describe the proposed housing at stake in this appeal first.
- proposed_units counts new, replacement, or additionally authorized dwellings
  whose permission is at stake, excluding dwellings whose existing authorization
  is settled. Physical existence alone does not settle authorization. Describe
  replacements explicitly; this count is not a claim of net housing growth.
- proposed_lots counts newly proposed residential building lots, not total parcels
  in a subdivision. Nonbuilding remainder/conservation land does not count.
Lots do not establish dwelling counts; explicit proposed residences can. Historical
totals are not current counts. If figures cannot be reconciled, leave the number
null and describe the conflict in scouting.

The municipal_decision is the decision challenged in this docket, not the later
court ruling. outcome: approved, approved_with_conditions, denied, other, unclear.
Appellants are those bringing this municipal challenge (including cross-appeals),
not every participant or every later appellate party. relationship: developer,
neighbor, organization, other, unclear. seeks: overturn_approval, overturn_denial,
change_conditions, other, unclear. representation: represented, pro_se, unclear;
absence of an attorney's name does not establish pro_se.
Classify seeks from the municipal action the appellant wants reversed: approval,
denial, or conditions. Challenges to enforcement or zoning-status determinations
use other unless the record establishes one of those specific permit actions.

## One discretionary-review finding per appeal

First write description: briefly state why the appellant challenged the municipal
decision, with evidence of the appellant's actual contentions. Then classify that
appeal basis, rather than classifying the project's permitting requirements.

involved=yes requires an evidenced connection between an actual appeal contention
and a municipal standard requiring evaluative judgment about adequacy,
compatibility, or reasonableness. A passage stating that such a standard exists
does not establish that connection. Fixed thresholds, disputed facts, legal
interpretation, procedural rights, and judicial review standards do not themselves
establish municipal discretion. Use no when the described appeal basis sufficiently
establishes no discretionary contention; use unclear when its basis or connection
to a discretionary standard is not established. Do not inventory individual grounds.

If involved=yes, central=yes requires evidence that discretionary review was a
principal basis of the appeal, not simply that the requested project or relief
was important. Use no for a clearly peripheral basis, otherwise unclear. Assess
the appeal as a whole; involvement and centrality may cite different discretionary
contentions. Supply central_evidence for a known centrality finding. A contention's
success, failure or procedural dismissal does not determine involvement or importance.
If involved=no use central=not_applicable; if involved=unclear use central=unclear.
Several discretionary issues share this one finding. Do not infer anti-housing motives.

## Sparse chronology and latest observed status

Record only milestones needed to follow the municipal litigation: filing,
judgment, further_appeal, reopening, remand, resolution. Dates retain stated
precision: YYYY-MM-DD, YYYY-MM, YYYY, or null. A date bound is not an occurrence
date. Start with the filing of this municipal appeal into court, using null when
its date is unknown. This timeline tracks litigation, not permit processing:
original applications and board decisions belong in municipal_decision or its
description, not as court filings or judgments. Include further review and continuation after remand, with their actual
dockets and bodies. Do not include an unrelated Act 250 timeline or routine
motions. Link related municipal disputes without merging their identities.
filing/further_appeal describe initiation of court review, never its later decision
date. judgment describes a court judgment; it does not by itself mean all litigation
ended. Omit intermediate motions unless needed to explain continuation.
remand describes sending the matter
back; resolution describes the latest observed conclusion. One event can describe
a judgment and remand together without duplicating its date as an appeal filing.
Include the latest observed court order even if nonfinal (kind=judgment), so the
timeline does not stop before an observed continuation or reopening.

observed_status describes the latest supported position across all documents.
For this study, litigation includes court proceedings and substantive municipal
review ordered on remand. It is ongoing when either remains pending, resolved when
an observed disposition concludes them, otherwise unclear. Include corrections,
reopening and further review; do not assert unobserved subsequent history.
Classify the action still required: a further judgment or a remand for a new merits
decision continues adjudication. Compliance with conditions of a granted permit
does not itself continue adjudication, even if plans need subsequent board approval.
An approval is therefore distinct from completion: a pending final judgment or
substantive remand keeps the matter open; ordinary permit compliance does not.
permission: upheld, changed, overturned, denied, unclear. Explain combinations
briefly (for example, approval with a ministerial remand). For a developer's
challenge, describe whether the municipal denial or conditions survived review.
Here upheld/changed/overturned refer to the municipal decision under review,
including a denial; denied means the court denied permission. The description
must make clear whether the observed outcome allows or prevents the proposal.
Do not calculate duration or stop history at the first judgment if it continues.

## Documented consequences

restrictions contains orders expressly suspending or prohibiting permission to
proceed, with known start/end dates. Ordinary permit conditions do not by themselves
establish such a restriction. A stay request or an inactive
case is not an established construction restriction. Describe remand requirements
in the timeline; also record a restriction only if one is established.
Distinguish withholding permission from an operative restraint: denial alone does
not establish an order to cease existing use, stop construction, or close a property.
housing_changes contains changes imposed relative to the proposal in this appeal,
with before/after counts only when supported. Conditions without numerical changes
use a description and null counts; do not copy unchanged project counts into
change fields. A permit denial is an outcome, not an
invented reduction to zero homes. Neither litigation duration nor permit outcome
establishes actual construction delay, actual homes built, or abandonment.

scouting is optional separate experimental feedback: uncertainties is an array of short
strings; suggestions an array of {idea,why}. It will not enter the final research
model. There are no grounds or conduct_findings collections.
