# Labeling guide

A label is the most likely true answer for one case, read from its documents
by a careful person with time. It is not what a pipeline would say, and not
a guess: where the documents don't establish a fact, the answer is `unknown`.
Labels use the results contract (`PIPELINE_CONTRACT.md`, `appeal-result-2`);
this guide says how to fill it. Read every document in the case, not only the
first pages.

Don't look at the legacy run's or any pipeline's answers while labeling. They are
compared afterwards; seeing them first anchors the label.

## The case

A case is one docket. Its documents are the decisions whose heading names
that docket; a decision issued in several dockets is shared, and only the
parts about this docket count. If a shared decision decides other dockets
but not this one, say so in a judgment note and take nothing from it beyond
what it says about this docket.

## Scope (every case)

`disposition` answers whether this docket is a municipal appeal about housing.

- `municipal_appeal`: `yes` when this docket appeals a municipal land-use
  decision (a board's or administrative officer's decision, a notice of
  violation upheld by a board) or continues one through further review. `no`
  for Act 250, state-agency permits, and enforcement the town or state brought
  without an appeal of a municipal decision. `unknown` when the documents
  don't settle it.
- `housing`: `yes` when the appeal concerns permission (including its
  conditions) for **new housing**: building a new structure that will serve as
  housing, or a major renovation that creates housing. This definition is the
  user's and decides scope here; it differs from the legacy pipeline's in
  places, so disagreements with the legacy run on these points are definition
  changes, not pipeline errors. `no` when the documents establish a nonhousing
  subject or a housing change that isn't new housing. `unknown` when the
  documents don't establish what is proposed.

  | Situation | housing |
  |---|---|
  | New house, duplex, apartment or mixed-use building with apartments | yes |
  | Single-family house converted to a duplex, or more units | yes |
  | Accessory apartment created inside an existing house | yes |
  | Replacement housing, even one unit for one, when a new structure is built or placed (a house rebuilt, a mobile home replaced) | yes |
  | Seasonal camp converted to a year-round home | yes |
  | Subdivision creating house lots, even with no house yet proposed | yes |
  | Care home or group home (a residence) | yes |
  | After-the-fact permit for a house already built, when the dispute prevents its occupancy | yes |
  | After-the-fact permit for a house already built, occupancy not at stake | no |
  | Appeal of a notice of violation, even about where a house sits | no |
  | House converted to a rental, rooms let to students, any change of occupancy or tenants without construction | no |
  | Large addition to a house that adds no unit | no |
  | Deck, garage, shed, fence, driveway or other accessory work at a house | no |
  | Hotel, inn, condo-hotel | no |
  | Commercial, industrial, extraction, telecommunications and other nonresidential uses | no |
- `value`: `excluded` when either is `no`; `in_scope` when both are `yes`;
  `uncertain` when neither is `no` and one is `unknown`.
- `reason`: one or two sentences on which facts decided it. `evidence`: the
  passages that establish the proposal and the appeal.

An excluded case has no records, and no review standard or other detail.
Labeling stops there.

## The record (in-scope and uncertain cases)

One record, for this docket only. Field meanings follow the contract and the
legacy drafting rules (`prompts/draft/legacy.md`); the points below are
where judgment is needed.

- **Project.** `proposed_units` counts the dwelling units the new housing
  would create, counting a replacement as one (say "replacement" in the
  description); `proposed_lots` counts new residential building lots. Use 0
  lots only when the documents make clear no new lots are created ("not
  proposed for subdivision"); otherwise `unknown`.
- **This docket only.** Never carry facts from another docket into this one,
  even when one decision covers both or a later docket's proposal superseded
  this one's. What the documents don't say about this docket is `unknown`.
  A subdivision's total lots is not its new residential lots. Use `unknown`
  when the count or its meaning isn't stated; never derive units from lots or
  bedrooms.
- **Municipal decision.** The board or officer action this docket challenges:
  its date (not the court filing date) and outcome. A ruling that an
  application is incomplete, or a refusal to reconsider, is `other`, and the
  appellant then `seeks` `other`.
- **Appellants.** Who brought this docket's appeal or cross-appeal, not every
  party. One entry per named person or organization (a couple is two
  entries); "X, et al." with the others unnamed leaves the list empty
  (unknown count). An enforcement action has no appellants: leave the list empty and
  note it. `pro_se` only when the documents say so.
- **Discretion.** `involved` is `yes` only when an actual contention in this
  appeal turns on an evaluative standard (compatibility, adequacy, character of
  the area) and the passages show it; `no` only when the appeal is established
  to turn on fixed rules, facts or legal rights; else `unknown`. `central`
  needs its own evidence that the discretionary contention was a principal
  part of the challenge.
- **Status.** `litigation` is `resolved` only with an observed conclusion and
  nothing shown pending; interim orders alone leave it `ongoing` or `unknown`.
  `permission` is the proposal's latest observed authorization in this docket.
  Never infer an outcome from a ruling on a motion.
- **Timeline and duration.** Start: the date the appeal was filed in court
  (`exact`) or the earliest dated event proving it existed (`on_or_before`).
  End: an observed conclusion of the whole review chain (`exact`) or the latest
  dated event proving it continued (`on_or_after`). Never date a filing from
  the docket number. Compute `kind` and `days` from the endpoints as the
  contract says.
- **Restrictions, housing changes, related dockets.** Only what is explicit:
  stop orders; changes imposed on the housing itself (units, size, design),
  not site conditions such as fences, curb cuts or construction hours;
  distinct proceedings named in
  the documents (including other dockets a shared decision decides, and a
  remand appealed again under a new number).
- **Review standard** (`appeal_review`). How the Environmental Court heard this
  docket's appeal of the municipal decision:
  - `on_the_record`: the court reviews the board's record and findings, usually
    because the town adopted on-the-record procedures under 24 V.S.A. § 4471
    ("This appeal is taken as an on-the-record appeal").
  - `de_novo`: the court hears the matter anew, taking its own evidence and
    deciding the application itself ("a de novo appeal", "the Court hears the
    application de novo", a site visit and evidentiary hearing on the merits).
  - `unknown`: not established. "De novo" describing the standard for summary
    judgment or for a question of law is not this; nor is the Supreme Court's
    standard of review.
  Cite the passage that establishes it.

Every answer other than `unknown`, and every list item, needs evidence: exact
quotations, each on the cited document's page, short and continuous. The check
command verifies every quotation.

## Which facts are labeled

`labeled.case` lists the scope facts labeled (normally all three).
`labeled.records` maps the docket to `all`, or to the fact keys labeled when
some were left unchecked. A fact that is labeled `unknown` is a real answer
("the documents don't establish it"); a fact not listed is simply unchecked.

## Judgments, disputes and questions

- `judgments`: for any fact that took judgment, `{"field": fact key,
  "confidence": "high" | "medium" | "low", "note": why}`. Always include one for
  `disposition`, `review_standard`, `duration` and any low-confidence fact.
- `disputed`: fact keys left unresolved after adjudication; they are not scored.
- `questions`: anything a person should decide.
- `history`: one entry per step, `{"step", "by", "at", "note"}`.

Fact keys are the comparison's: `disposition`, `municipal_appeal`, `housing`,
`duration`, `start`, `end`, `decision_outcome`, `decision_date`, `litigation`,
`permission`, `review_standard`, `discretion_involved`, `discretion_central`,
`appellants`, `representation`, `municipality`, `units`, `lots`,
`restrictions`, `housing_changes`, `related`.
