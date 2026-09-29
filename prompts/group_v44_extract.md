## Step 2 of 2: extraction

Extract the case history, using only documents you marked `belongs` `yes` or `unclear`. The fields serve six research questions: how long each stage took; who appealed and where; on what grounds; what the courts did, including explicit criticism or sanctions; whether approval was stayed or remanded; and whether the housing proposal changed. Record only what the documents support. Keep summaries under 30 words.

Return one flat JSON object. Its keys, in this order, are:

{"schema_version":"v4.4","project":null,"processes":[],"participants":[],"grounds":[],"remarks":[],"events":[],"uncertainties":[],"suggestions":[]}

Every participant, ground, remark, and event has `p`, the 1-based position of its process in `processes`. When that process lists more than one docket, every event, ground, and remark must also give `dockets`: an array of ALL and ONLY the docket numbers it concerns. Never select just the first of several affected dockets. For a ruling with different outcomes or finality in different dockets, make separate event entries for the distinct dispositions, each with the correct docket subset. Do not duplicate a common disposition merely to name each docket. If attribution cannot be established, use an empty array and record an uncertainty; never assign it to all dockets by default.

### `project`

The development at issue, or null if none: `{summary, municipality, address?, units?, lots?, evidence}`.

- `units`: an integer count of new dwelling units proposed in the application being appealed. Use it only when a count is stated, or when a clearly described building type fixes it (a single-family home is 1, a duplex is 2). If the application does not itself seek approval for the dwellings, omit `units`. For example, a subdivision whose future homes need separate permits gets `lots`, not `units`.
- `lots`: an integer count of newly created residential lots proposed in the current application, when homes are not counted. Exclude retained existing lots; use a stated net-new count, or an unambiguous total minus stated existing count. Never copy a lot count into `units`, or copy an earlier subdivision's lots into a later application for one dwelling.
- Do not count existing homes. If the proposal changed, describe the version before the court, and record changes as `effects`.
- If a residence is mentioned but its new/existing status is unknown, omit `units` and `lots`. A prior unappealed permit is not the application currently being appealed. In an NOV appeal without an application seeking dwelling approval, describe the alleged conversion or dwelling in prose and omit numerical proposed-unit counts.

### `processes`

One object per decision-making level, in chronological order: `{level, body, dockets?, applicant?}`.

- `level`: `municipal`, `act_250`, `state_agency`, `environmental_division`, `supreme_court`, or `other`.
- `body`: the decision maker's name as printed in the document, normalizing capitalization only. Do not substitute a later-era court name for `Vermont Environmental Court`.
- `dockets`: that level's docket numbers, each case once. Treat `22-ENV-124` and `22-ENV-00124` as the same case. Reporter citations such as `2005 VT 126` are not docket numbers; omit them.
- Conflicting header/footer numbers are not proof of two proceedings. Where the source describes one appeal, retain its supported caption number and flag conflicting numbers as uncertainty unless the text establishes a second case.
- `applicant`: the permit applicant's name, for municipal, Act 250, or agency levels.
- Include an earlier level even if the documents only mention its decision.

### `participants`

Parties to a court level, not judges, courts, or witnesses: `{p, name, kind, roles, relation, members?, counsel?, attorneys?, evidence}`.

- `kind`: `person`, `business`, `group`, `municipality`, `agency`, or `other`.
- `roles`: one or more of `appellant`, `cross_appellant`, `applicant`, `interested_person`, or `other`.
  - Do not assume exactly two sides.
  - A municipality defending its own decision is an `interested_person`.
  - Roles are specific to each level: at the Supreme Court, the appellant is whoever appealed the Environmental Division's decision.
- `relation`: the participant's relation to the project: `developer`, `neighbor`, `resident_group`, `advocacy_group`, `municipality`, `agency`, `other`, or `unclear`. Use `neighbor` only when the documents say the participant owns or lives on nearby property.
- `members`: individual names, when several people act as one party.
- Represent each party once per level. Co-applicants or co-appellants who act together, for example with shared counsel, are one participant with `members`; never list a member again as a separate participant at the same level.
- A municipality that did not appear is omitted.
- Do not infer participation in a higher court from property ownership or participation below. Retain supported earlier applicant/appellant roles when recording that earlier appeal, even if the current matter is enforcement.
- `counsel`: `represented` or `pro_se`, only when stated. Absent counsel information is unknown.
- Receiving a court notice personally or being listed without an attorney is not an express pro se statement. Do not infer counsel status from either.
- `evidence`: required.

### `grounds`

The main arguments about whether the appealed decision should stand, from the statement of questions, briefs, or decision, usually one to eight per court level. Merge related numbered questions.

- A procedural objection to the decision under appeal is a ground. A challenge to the whole appeal on standing, jurisdiction, or timeliness is also a ground.
- Motions about how the case proceeds are not grounds. These include motions to strike, amend, supplement, extend, and to dismiss one party. Their rulings are `events`. For example, a motion to dismiss one appellant for lack of party status is a `ruling` event, not a ground.
- When a question is dismissed on a motion, record the question itself as the ground, with result `dismissed`. Do not add the motion as another ground.
- Record a ground only when the documents state the argument. Do not reconstruct one from the decision appealed.
- Record a ground under the level where it was argued. If it is argued again on appeal to a higher court, add it again under that level.
- Each ground is `{p, summary, raised_by, topic, category, result, applies?, reason?, evidence}`.
  - `raised_by`: the participant name that made the argument.
  - `topic`: `use`, `density`, `dimensional`, `design`, `traffic`, `character`, `environment`, `infrastructure`, `procedure`, `standing`, `jurisdiction`, or `other`.
  - `category`, for the criterion the ground relies on:
    - `objective`: a measurable standard such as a setback, lot size, or height.
    - `discretionary`: a judgment standard such as character of the area, compatibility, or town plan policy.
    - `procedural`: notice, timeliness, jurisdiction, or standing.
    - `mixed` or `unclear`.
  - `result`, from the viewpoint of the party that raised it: `succeeded`, `failed`, `dismissed` (struck, or not reached for procedural or jurisdictional reasons), `withdrawn`, `undecided`, or `mixed`.
    - Use `succeeded` or `failed` only when a court decided the argument.
    - A ruling that only lets a question proceed, such as denying a motion to dismiss or strike it, leaves it `undecided`. A question deferred to trial or to further filings is also `undecided`.
    - `mixed` means the court decided it partly for and partly against the raiser.
    - Update the result from any later document in the group, including a higher court's account of what the lower court decided.
    - Decide each argument's result separately from the party's overall win or loss. An argument the court accepts is `succeeded` even if the party loses on another issue. An issue expressly reserved for remand is `undecided`. An advisory issue the court refuses to reach is `dismissed`, not `failed`.
    - Split materially different arguments when one is resolved and another remains undecided. Include material arguments recounted for the lower court; do not leave grounds empty merely because a later ruling dominates the group.
    - Check that summary accurately states the raiser's position and reason states the court's response. Do not reverse the argument or contradict the stated result.
  - `applies`: `false` only when the court says the cited standard does not govern the decision, such as aspirational town plan language. Omit it otherwise.
  - A non-applicable standard must carry `applies:false` even when the court discusses an alternative merits analysis. Constitutional tests are not discretionary land-use standards merely because they involve judgment; use `mixed` or `unclear` when the objective/procedural categories do not fit. Safety criteria phrased as unreasonable or unsafe impacts are discretionary unless a measurable threshold is stated.
  - `reason`: the court's reason, briefly.

### `remarks`

Only statements in which the court expressly criticizes, warns, or sanctions a litigant for how it conducted the litigation: `{p, kind, target, summary, evidence}`.

- Examples: calling a filing frivolous, in bad faith, or abusive; repeated failures to follow rules or orders; misrepresentation; awarding costs or fees as a sanction.
- Not remarks:
  - the court disagreeing with an argument, or finding it unsupported, unpreserved, or inadequately briefed;
  - explaining procedure or correcting a litigant's misunderstanding;
  - criticizing the municipal body or a lower court;
  - administrative directions, such as fixing exhibit labels or formatting;
  - routine procedural rulings.
- `kind`: `criticism`, `sanction`, `fees`, `warning`, or `other`.
- `target`: the participant's name.
- Most cases have none.

### `events`

A concise chronology, in date order within each process: `{p, date?, kind, summary, outcome?, final?, mechanism?, effects?, conditions?, evidence}`.

- `date`: `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`, only as precisely as a document states it for that event. Never infer a month from surrounding dates. For a written decision, use the date it was issued, not the hearing date.
- `kind`:
  - `application`: an application filed.
  - `decision`: a permit or enforcement decision by a municipal or state body.
  - `appeal`: an appeal filed into this level. The summary names the appellant and the decision appealed.
  - `stay`: a stay requested, granted, or denied; say which.
  - `ruling`: a court ruling. Record each document's own ruling, and earlier rulings it recounts, once each.
  - `settlement` or `withdrawal`.
  - `other`.
- Leave out routine filings, motion filings, hearings, site visits, conferences, stipulated scheduling changes, and property history.
- A court ruling that sends a case back is a `ruling` with outcome `remanded`. The lower body's new decision is a later `decision`.
- `outcome`, for `decision` and `ruling`: `approved`, `approved_with_conditions`, `denied`, `affirmed`, `reversed`, `remanded`, `dismissed`, `mixed`, `interim` (a ruling that does not decide the permit or end the case), or `other`.
  - A partial summary-judgment ruling leaving the permit merits for trial is `interim`, even when it resolves some legal issues. Do not repeat the underlying dismissal as the outcome of a later motion seeking relief from that judgment; describe the postjudgment denial separately.
  - Use `approved_with_conditions` only when the documents say that body imposed conditions.
  - For a `stay`, the outcome is `granted`, `denied`, or `mixed`.
- `final`, for a `ruling`: `true` if it was a final judgment ending the proceeding at this level when issued, even if a later appeal reopens it; `false` otherwise.
- `mechanism`: only when `final` is `true`, or when the ruling dismisses an appeal or an appellant. One of `merits`, `summary_judgment`, `standing`, `jurisdiction`, `timeliness`, `procedural`, `stipulation`, `withdrawal`, or `other`.
- `effects`, for a `decision` or `ruling` that changes the housing proposal relative to what was applied for, a list of `{measure, change, from?, to?, summary}`:
  - `measure`: `units`, `lots`, `floor_area`, `land`, or `other`. Use `units` whenever dwelling units or unit types are affected, and `lots` whenever residential lots are created, voided, or changed.
  - `change`:
    - `removed`: a component denied or struck.
    - `reduced` or `increased`.
    - `conditioned`: approved subject to a limiting condition.
    - `restored`: a limiting condition imposed below was struck.
    - `other`.
  - Include only changes the documents state. A complete denial is recorded by the outcome `denied`, not as an effect.
  - `from` and `to` are numbers.
- `conditions`: a short summary of conditions imposed, when stated.

### Evidence

Each object's `evidence` is one or two quotes of at most 25 words, copied exactly from one continuous passage on the cited page. Never join separate sentences. The quote must itself support the claim; for example, cite the sentence that grants a stay, not a nearby sentence.

### Other keys

- `uncertainties`: short strings, only for material ambiguity about grouping, parties, dates, grounds, outcomes, or unit counts.
- `suggestions`: a list of `{idea, why}` for facts this schema misses that matter to the research questions. Usually empty.
