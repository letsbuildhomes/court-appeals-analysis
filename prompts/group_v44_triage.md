## Step 1 of 2: triage

Decide which documents belong together and whether the group is in the study's scope. Keep this step short. Return:

{"schema_version":"v4.4-triage","documents":[{"doc":1,"caption_dockets":[],"date":"YYYY-MM-DD","belongs":"yes"}],"matter":"","case_type":"unclear","development":"unclear","municipal_appeal":"unclear","housing":"unclear","reason":"","evidence":[]}

- `documents`: one entry per document, in `doc` order.
  - `caption_dockets`: docket numbers printed in that document's own caption as the case being decided, copied as printed. Exclude dockets that appear only in citations.
  - `date`: the date the document was issued or signed, as `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`; omit if not stated. If one PDF contains several orders, use the latest date.
  - `belongs`: `yes` if the document is about the group's main matter, meaning the same docket or the same property and dispute, including consolidated or coordinated dockets and appeals of it; `no` if it is about a different matter; `unclear` otherwise. The main matter is the one most documents address.
  - `note`: one short sentence, only when `belongs` is `no` or `unclear`.
- `matter`: one or two plain sentences naming the development, municipality, decision appealed, and appellant.
- `case_type`: the main matter's kind.
  - `municipal_permit_appeal`: an appeal of a municipal decision on a permit or approval application.
  - `municipal_enforcement`: municipal enforcement, or an appeal of a notice of violation.
  - `act_250`: an Act 250 permit or jurisdictional opinion.
  - `state_permit`: another state agency permit.
  - `state_enforcement`: ANR or state enforcement.
  - `bylaw_or_policy`: a challenge to a bylaw or plan itself.
  - `other` or `unclear`.
- `development`: what the application or action under appeal would build or change. Judge it by that application, not by units the property already has. Units approved by an earlier, unappealed permit are existing.
  - First identify exactly which decision is now appealed. A later landscaping, access, or other condition-only application does not add housing merely because a separate, unappealed permit previously approved a house. Distinguish that from an appeal of the new-home permit itself, whose conditions are disputed.
  - Mere mention of a residence or rental apartment does not show it is newly proposed. If the source does not establish whether housing is new or existing, use `unclear` for development and housing, rather than guessing either yes or no.
  - For enforcement/variance appeals, distinguish an alleged new dwelling or conversion whose authorization remains disputed from maintenance of an established year-round home. If the boundary is unclear, continue as `unclear`; do not exclude solely because construction preceded enforcement.
  - Use one of the enum labels below, never a descriptive sentence as `development`.
  - `new_housing`: new or additional dwelling units, homes, apartments, or residential lots. This includes:
    - a new home on a vacant or newly created lot, even when the dispute concerns its permit conditions;
    - an accessory dwelling unit;
    - conversion of a seasonal camp or non-residential building into a year-round dwelling.
  - `existing_home`: changes to an existing year-round home that add no dwelling unit, including replacing, rebuilding, enlarging, or adding a deck, garage, or home business.
  - A senior, assisted-living, or care facility with individual apartments or dwelling units is `new_housing`, or `mixed_with_housing` when care beds are also included. A facility with only beds or rooms is `institutional`.
  - `commercial`, `industrial` (including extraction), `institutional`, `infrastructure`, `agricultural`, `mixed_with_housing`, `other`, or `unclear`.
- `municipal_appeal`: `yes` if the main matter includes an appeal of a municipal land-use decision, such as one by a zoning administrator, development review board, zoning board, or planning commission, including municipal enforcement about a development. `no` if the only decisions appealed are Act 250 or state decisions, or if it is a direct enforcement action with no municipal land-use decision. `unclear` otherwise.
  - An appeal of a municipal notice of violation qualifies; a direct suit to enforce a permit or an earlier court judgment does not become a municipal appeal because its historical background mentions a closed appeal. Judge the current matter, not every proceeding recounted in its history.
- `housing`: `yes` for `new_housing` or `mixed_with_housing`; `no` when the development clearly adds no dwelling unit; `unclear` otherwise.
- `reason`: one sentence explaining the scope answers.
- `evidence`: one or two short quotes supporting them.

Answering `no` to either scope question ends processing of this group, so answer `unclear` rather than `no` when the documents do not settle it.
