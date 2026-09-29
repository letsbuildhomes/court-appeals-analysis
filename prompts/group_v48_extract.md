## Step 2: extract the observed case history

Use only documents whose membership is yes or unclear. The research concerns elapsed legal proceedings, who appealed and where, grounds and court treatment, express conduct criticism, stays/remands, and changes to proposed housing. The PDFs are incomplete observations, not complete dockets or evidence of construction delay.

Return this flat object, in this order:

{"schema_version":"v4.8","project":null,"processes":[],"participants":[],"grounds":[],"remarks":[],"events":[],"uncertainties":[],"suggestions":[]}

## Identity, scope, and support

Separate three things throughout: the application/action currently appealed; earlier proceedings recounted as history; and what this court actually decides. A fact about one is not automatically a fact about another. Keep summaries under 30 words.

Every participant, ground, remark, and event has `p`, the 1-based position in `processes`. When its process has several dockets, each event, ground, and remark has `dockets`, an array of all and only the affected dockets. If applicability is unknown use [] and note uncertainty. Split a ruling only where docket subsets have different outcomes or finality; do not duplicate a shared disposition.

Each factual object except a process has `evidence`: one or two `{doc,page,quote}` objects. Use an exact continuous passage of at most 25 words per quote. Choose support for the recorded interpretation, not merely shared names. Use separate quotes for separate passages. Omit unsupported optional facts. An uncertainty note never licenses an unsupported categorical value.

## Project and processes

`project`: `{summary,municipality,address?,units?,lots?,evidence}`, or null.

Choose the object of the current application before recording a quantity. Permission to construct dwellings supplies `units`; permission to divide land supplies `lots`. A residential building lot is land, not an approved dwelling. Populate both only if both approvals are actually sought. Count newly proposed objects, not existing stock or historical proposals. A dwelling application for a single-family home or duplex fixes 1 or 2 units; lot counts never imply dwelling counts. Both fields are nonnegative integers. Omit a count unless this application and its quantity are established; an enforcement allegation alone supplies neither. Describe disputed facts as allegations. Record supported changes separately as event effects.

`processes`: one `{level,body,dockets?,applicant?}` per decision-making level in chronological order, including earlier levels described in the documents.

- `level`: municipal, act_250, state_agency, environmental_division, supreme_court, other.
- `body`: the institution's name as printed, with capitalization normalized, preserving historical identity.
- `dockets`: actual proceeding identifiers at this level. Normalize equivalent formats into one case. A reporter citation is not a docket; conflicting header numbers alone do not establish two proceedings. Preserve unresolved identity as uncertainty.
- `applicant`: applicant at municipal/agency/Act 250 level, when stated.

## Participants

`participants`: parties who appeared at a court level, each `{p,name,kind,roles,relation,members?,counsel?,attorneys?,evidence}`.

- `kind`: person, business, group, municipality, agency, other.
- `roles`: array of appellant, cross_appellant, applicant, interested_person, other. Roles belong to this court level. A municipality defending its decision is interested_person. There need not be exactly two sides.
- `relation`: developer, neighbor, resident_group, advocacy_group, municipality, agency, other, unclear. Neighbor requires stated nearby ownership/residence.
- People acting as one party may be grouped with `members`; do not also list those same members separately at that level.
- `counsel`: represented or pro_se, only expressly supported; omission means unknown. A notification list or absent attorney name proves neither.
- Party appearance, roles, and representation do not propagate from a lower to a higher court. Judges and witnesses are not parties.

## Grounds: one claim with one judicial treatment

`grounds`: material arguments about whether the appealed decision or the whole appeal can stand, normally one to eight per court level. Each is `{p,raised_by,summary,topic,category,reason?,result,applies?,evidence}`.

Each ground binds a speaker to a proposition: identify `raised_by`, then summarize what that same speaker asserts, then record the court's response in `reason`. Read the pair as “[raised_by] argues that [summary]”; both must match the source together. Include only stated arguments, including earlier arguments recounted later. Use `unknown` if the argument is established but its raiser is not. A court's holding alone does not identify who argued for it.

Make each summary a proposition that the court can accept or reject, rather than a request to grant a motion. Separate a rule's interpretation from whether the facts satisfy it. Merge propositions only when their treatment matches. Write the court's reason, then choose result for that exact proposition using the latest observed ruling at this level. A later correction supersedes the earlier interpretation; do not average their outcomes. A higher court's account can supply evidence of the lower court's treatment.

- `topic`: use, density, dimensional, design, traffic, character, environment, infrastructure, procedure, standing, jurisdiction, other.
- `category` describes the governing criterion: objective (measurable threshold), discretionary (land-use judgment standard), procedural (notice, standing, jurisdiction, timeliness), mixed (several types), unclear (not established or not fitting these types).
- `result` belongs to this exact proposition, not the motion or case containing it. Use its latest substantive or issue-specific treatment: accepted = succeeded; rejected on substance = failed; inseparably accepted in part = mixed; this argument itself withdrawn = withdrawn; court excludes this question from consideration without deciding its substance = dismissed; otherwise not resolved = undecided. Whole-case withdrawal, settlement, or dismissal does not adjudicate unresolved underlying propositions. A deferred or factually unresolved merits question stays undecided even after the proceeding ends. The reason and result must describe the same judicial treatment.
- `applies:false` only if the court expressly finds the invoked criterion does not govern. Otherwise omit.
- `reason`: concise judicial reasoning when stated.

A motion is a procedural vehicle, not another ground. Record its ruling as an event. A challenge to the whole appeal's standing/jurisdiction/timeliness is a ground; case-management motions and individual-party dismissal motions are events. When a question is dismissed, record that question's disposition without adding its motion as a duplicate ground.

## Events: what happened, when, and what ended

`events`: dated chronology within each process, each `{p,date?,kind,summary,outcome?,final?,mechanism?,effects?,conditions?,evidence}`.

- `date`: YYYY-MM-DD, YYYY-MM, or YYYY, only at stated precision. A written order uses its issue/signature date. Do not infer event dates from docket numbers; the export handles docket-month fallback separately.
- `kind`: application, decision (municipal/agency), appeal (filed into this level), stay, ruling (court), settlement, withdrawal, other.
- Include material earlier applications/decisions/appeals and each observed ruling once. Omit routine filings, hearings, site visits, scheduling, and property history. Distinguish an earlier judgment from a later ruling on relief from that judgment.
- For court rulings, determine `final` first: true if the affected docket proceeding ended at this level when issued, even if later reopened; false otherwise. Deciding a ground does not itself end a proceeding.
- `outcome` is required for court rulings and describes the permit/proceeding disposition. An express remand is remanded. Otherwise a court ruling with final=false is interim, regardless of how many motions or grounds it resolves. For completed dispositions choose approved, approved_with_conditions, denied, affirmed, reversed, dismissed, mixed, or other. mixed means a divided final disposition, not partial success on an intermediate motion. A subsequent municipal decision is another event.
- `mechanism`: merits, summary_judgment, standing, jurisdiction, timeliness, procedural, stipulation, withdrawal, other. This explains how an appeal/appellant was disposed of. Omit for interim rulings; name their motion in summary instead.
- Stay events state requested/granted/denied in summary; a decided stay has outcome granted, denied, or mixed. A request is not an operative stay.
- `conditions`: stated conditions imposed, briefly.

`effects`: changes imposed on a proposal relative to the application, attached to the decision/ruling that imposed them. Each is `{measure,change,from?,to?,summary}`. Measures: units, lots, floor_area, land, other. Changes: removed, reduced, increased, conditioned, restored, other. Numeric from/to only when supported. A complete denial is an outcome, not a separate housing effect. Preserve effects of historical decisions as historical events; do not transfer their quantities into the current project.

## Remarks and scouting feedback

`remarks`: explicit court criticism, warnings, sanctions, or fees directed at a litigant's litigation conduct. Each `{p,kind,target,summary,evidence}`; kind is criticism, sanction, fees, warning, other. Distinguish threatened from imposed sanctions. An adverse ruling, weak argument, procedural explanation, criticism of a lower body, or underlying zoning penalty is not a conduct remark. Most cases have none.

`uncertainties`: short strings recording material unresolved ambiguities. `suggestions`: `{idea,why}` observations about research-relevant gaps in the schema. These two fields are scouting feedback only and will be removed from the final production prompt/model; they are not research facts.
