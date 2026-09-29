## Step 1 of 2: triage

Decide which documents belong together and whether the group is in the study's scope. Keep this step short. Return:

{"schema_version":"v4-triage","documents":[{"doc":1,"caption_dockets":[],"date":"YYYY-MM-DD","belongs":"yes","note":""}],"matter":"","municipal_appeal":"unclear","housing":"unclear","reason":"","evidence":[]}

- `documents`: one entry per document, in `doc` order.
  - `caption_dockets`: docket numbers printed in that document's own caption as the case being decided, copied as printed. Exclude dockets that appear only in citations.
  - `date`: the date the document was issued or signed, as `YYYY-MM-DD`, `YYYY-MM`, or `YYYY`; omit if not stated.
  - `belongs`: `yes` if the document is about the group's main matter, meaning the same docket or the same development and dispute, including consolidated or coordinated dockets and later appeals of it; `no` if it is about a different matter; `unclear` otherwise. The main matter is the one most documents address.
  - `note`: optional, one short sentence, required when `belongs` is `no` or `unclear`.
- `matter`: one or two plain sentences naming the development, municipality, decision appealed, and appellant.
- `municipal_appeal`: `yes` if the main matter includes an appeal of a municipal land-use decision, such as one by a zoning administrator, development review board, zoning board, or planning commission, including municipal enforcement about a development. `no` if the only decisions appealed are Act 250, state agency, or other non-municipal permits, or if it is a direct enforcement action with no municipal land-use decision. `unclear` otherwise.
- `housing`: `yes` if the development proposes new or additional dwelling units. This includes homes, apartments, residential subdivision lots, conversions that add units, and mixed use with housing. `no` if the development is non-residential, or concerns an existing home without added units, such as a deck, garage, setback dispute, home business, or occupancy of an existing unit. `unclear` otherwise.
- `reason`: one sentence explaining the two scope answers.
- `evidence`: one to three quotes supporting the scope answers.

Answering `no` to either scope question ends processing of this group, so answer `unclear` rather than `no` when the documents do not settle it.
