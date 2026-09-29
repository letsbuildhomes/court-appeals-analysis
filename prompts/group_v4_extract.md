## Step 2 of 2: extraction

Extract the case history, using only documents you marked `belongs` `yes` or `unclear`. The fields serve six research questions: how long each stage took; who appealed and where; on what grounds; what the courts did, including explicit criticism or sanctions; whether approval was stayed or remanded; and whether the housing proposal changed. Record only what the documents support. Return:

{"schema_version":"v4","project":null,"processes":[],"uncertainties":[],"suggestions":[]}

### `project`

The development at issue, or null if none: `{summary, municipality, address?, units?, lots?, evidence}`.

- `summary`: one or two sentences, including the building type in the documents' words.
- `units`: an integer count of new dwelling units proposed in the application being appealed. Use it only when a count is stated, or when a clearly described building type fixes it (a single-family home is 1, a duplex is 2).
- `lots`: an integer count of residential lots proposed, for subdivisions where the number of homes is not stated. A lot is not a dwelling unit; never copy a lot count into `units`.
- Do not count existing homes. If the proposal changed during the case, describe the version before the court, and record changes as ruling `effects`.

### `processes`

One object per decision-making level, in chronological order: `{level, body, dockets?, applicant?, events, participants?, grounds?, remarks?}`.

- `level`: `municipal`, `act_250`, `state_agency`, `environmental_division`, `supreme_court`, or `other`.
- `body`: the decision maker's normalized name, such as `Town of Essex Planning Commission` or `Vermont Superior Court, Environmental Division`.
- `dockets`: court case numbers at this level, copied as printed. Omit for an unnumbered municipal decision.
- `applicant`: the permit applicant's name, for municipal, Act 250, or agency levels.
- Include an earlier level even when the documents only mention its decision.

`events` is a concise chronology for the level. Each event is `{date?, kind, summary, outcome?, final?, mechanism?, effects?, conditions?, evidence}`.

- `date`: `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`, with no guessed precision.
- `kind`:
  - `application`: an application filed.
  - `decision`: a permit or enforcement decision by a municipal or state body.
  - `appeal`: an appeal filed into this level. The summary names the appellant and the decision appealed.
  - `stay`: a stay requested, granted, or denied; say which.
  - `remand`: a case sent back to a lower body.
  - `ruling`: a court ruling. Record each group document's own ruling as an event of its level, and also earlier rulings it recounts.
  - `settlement` or `withdrawal`: the case or an appeal ending by agreement or withdrawal.
  - `other`: any other material event.
- Leave out routine filings, motions (the ruling on them is the event), hearings, conferences, and property history.
- Record each real-world event once, even when several documents recount it.
- `outcome`, for `decision` and `ruling`: `approved`, `approved_with_conditions`, `denied`, `affirmed`, `reversed`, `remanded`, `dismissed`, `mixed`, `interim` (a ruling on motions or questions that does not decide the permit or end the case), or `other`.
- `final`, for a `ruling`: `true` if it ends the proceeding at this level, `false` if it does not.
- `mechanism`, for a ruling that ends the proceeding or dismisses the appeal: `merits`, `summary_judgment`, `standing`, `jurisdiction`, `timeliness`, `procedural`, `stipulation`, `withdrawal`, or `other`.
- `effects`, for a `decision` or `ruling` that changes the housing proposal: a list of `{measure, change, from?, to?, summary}`.
  - `measure`: `units`, `lots`, `floor_area`, `land`, or `other`.
  - `change`: `removed`, `reduced`, `increased`, `conditioned`, or `other`.
  - Include only changes the documents state. A missing effect means not stated, not no effect.
- `conditions`: a short summary of conditions imposed, when stated.

`participants` is used for court levels. It lists litigation participants, not the judge or court: `{name, kind, roles, relation, members?, counsel?, attorneys?, evidence}`.

- `kind`: `person`, `business`, `group`, `municipality`, `agency`, or `other`.
- `roles`: one or more of `appellant`, `cross_appellant`, `applicant`, `interested_person`, or `other`. Do not assume exactly two sides, or that the municipality opposes the appellant.
- `relation`: the participant's relation to the project: `developer`, `neighbor`, `resident_group`, `advocacy_group`, `municipality`, `agency`, `other`, or `unclear`.
- `members`: the individual names when the court treats several people as one party, such as "Neighbors".
- `counsel`: `represented` or `pro_se`, only when stated. The absence of counsel information is unknown.
- `attorneys`: attorney names, when stated.

`grounds` is used for court levels. It lists the few material arguments against or for the decision, usually one to eight. Merge related numbered questions; do not list every question: `{summary, raised_by?, topic, category, result, applies?, reason?, evidence}`.

- `topic`: `use`, `density`, `dimensional`, `design`, `traffic`, `character`, `environment`, `infrastructure`, `procedure`, `standing`, `jurisdiction`, or `other`.
- `category`, for the criterion the ground relies on:
  - `objective`: a measurable standard such as a setback, lot size, or height.
  - `discretionary`: a judgment standard such as character of the area, compatibility, or town plan policy.
  - `procedural`: notice, timeliness, jurisdiction, or standing.
  - `mixed` or `unclear`.
- `result`, for the ground as of the latest document: `succeeded` (the argument prevailed), `failed`, `dismissed` (struck or not reached for procedural reasons), `withdrawn`, `undecided`, or `mixed`.
- `applies`: `false` only when the court says the cited standard does not govern the decision, such as aspirational town plan language. Omit it otherwise. Keep this separate from `result`.
- `reason`: the court's reason, briefly, when stated.

`remarks` is used for court levels. It records only explicit judicial criticism of a litigant's conduct or filings, sanctions, fee awards, or warnings: `{kind, target?, summary, evidence}`.

- `kind`: `criticism`, `sanction`, `fees`, `warning`, or `other`.
- Do not infer frivolousness from a loss, a dismissal, pro se status, or a discretionary argument.

### Other keys

- `uncertainties`: short strings, only for material ambiguity about the documents' grouping, parties, dates, grounds, outcomes, or unit counts.
- `suggestions`: a list of `{idea, why}` for facts this schema misses that matter to the six research questions. Leave it empty when nothing is missing.
