## Step 1 of 2: membership and scope

Identify the proceeding to which these orders belong and the application/action that gave rise to it. Then answer two independent scope questions: does this proceeding include a municipal land-use appeal, and does the appealed subject add housing? A document's procedural phase does not change the proceeding's identity. Do not transfer a separate historical proceeding's scope into it. Return:

{"schema_version":"v4.10-triage","documents":[{"doc":1,"caption_dockets":[],"date":"YYYY-MM-DD","belongs":"yes"}],"matter":"","case_type":"unclear","development":"unclear","municipal_appeal":"unclear","housing":"unclear","reason":"","evidence":[]}

## Membership and identity

`documents`: one entry per supplied doc, in order. `caption_dockets` copies identifiers from that document's own caption, excluding cited cases. `date` is its latest issue/signature date at stated precision (YYYY-MM-DD, YYYY-MM, or YYYY); omit if absent. `belongs` is yes for the same docket or property/dispute, including coordinated dockets and appeals of it; no for a different matter; unclear if unresolved. The main matter is the one most documents address. Give a short `note` only for no/unclear.

`matter`: one or two sentences identifying this proceeding's originating application/action, decision under appeal, municipality, and appellant. Distinguish those from separate approvals and proceedings recounted as background.

## Current proceeding

`case_type`: municipal_permit_appeal (municipal application/permit decision), municipal_enforcement (municipal enforcement or notice-of-violation appeal), act_250, state_permit, state_enforcement, bylaw_or_policy, other, or unclear.

`municipal_appeal`: yes when this proceeding arose from review of a municipal land-use decision, including an appealed notice of violation. Later orders within that appeal retain its identity, including post-judgment proceedings. No for only state/Act 250 matters or a separately initiated enforcement suit without municipal review; a historical appeal cited in a separate suit does not make the suit an appeal. Determine this from the described procedural relationship, not the type of motion currently decided. Use unclear when the relationship is not established.

## Subject of this application/action

`development` describes what the application/action giving rise to this appeal would authorize or prohibit, relative to the previously established use. It is not everything on the property or the effect of the latest procedural motion. Separate a new-housing permit whose conditions are disputed from a later application modifying a condition of an already approved, unappealed dwelling permit.

- new_housing: additional dwellings or residential building lots, including a new home on a vacant lot, an ADU, and conversion of a seasonal camp or nonresidential building to year-round housing.
- existing_home: work on established year-round housing that adds no dwelling unit, including replacement, enlargement, or ancillary uses.
- mixed_with_housing: housing additions plus other uses.
- commercial, industrial (including extraction), institutional, infrastructure, agricultural, other, or unclear.

Care facilities count as housing when they have apartments/dwelling units; beds or rooms alone are institutional. The authorization sought or disputed controls: prior construction does not itself establish that a dwelling is legally existing. A residence's mere presence does not establish that it is new either. If new/existing status is unresolved, use unclear.

`housing`: yes for new_housing/mixed_with_housing; no when the current subject clearly adds no housing; unclear otherwise. `reason`: one sentence explaining both scope answers. `evidence`: one or two `{doc,page,quote}` objects with exact short supporting passages.

No to either scope question ends processing. Use unclear when evidence does not settle a scope question, so the matter proceeds for extraction and review.
