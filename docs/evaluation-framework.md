# Paired evaluation framework

The framework reads results under the output contract
([PIPELINE_CONTRACT.md](../PIPELINE_CONTRACT.md)), never draft records. It
executes no extraction or reference-generation strategy.

```python
from pipeline import runs
from appeal_reference import read_reference
from appeal_evaluation import evaluate

baseline = runs.load('data/runs/production', case_ids)
candidate = runs.load('data/experiments/NAME/run', case_ids)
reference = read_reference('/path/to/reference.json')
result = evaluate(baseline, candidate, reference)
```

Both runs use the same reference release. A case identity alone is insufficient:
source hashes must also agree. Incompatible and missing cases remain visible.
`reference=None` requests an unscored comparison; absent labels never imply
correctness. This framework does not claim to validate the truth of supplied
human labels or verify source bytes merely from their recorded hash.

`appeal-reference-1` holds a reference ID, version, purpose, and cases. Each case
has its frozen source hash, adjudication state, case-level `fields`, expected
`records`, and `records_complete`. A record has a reference-local `record_id`,
`identity.docket`, and field expectations. Field paths are arrays of object
keys, relative to the case or record. Each expectation includes its state,
comparison rule, evidence, rationale, and label version. Adjudicated expectations
include a value; null is a value, distinct from an absent output field.

```json
{
  "path": ["duration", "days"], "state": "adjudicated", "value": 30,
  "comparison": "exact", "version": "rubric-1",
  "evidence": [{"quote": "The synthetic interval lasted 30 days."}],
  "rationale": "Invented software-test source explicitly states the interval."
}
```

Only adjudicated fields in adjudicated cases can score. `pending`, `disputed`,
and `unknown` label states remain unscored. An adjudicated expected value
`"unknown"` is different: it is a reviewed semantic answer. `exact` compares
structured values; `unordered` compares collections without positional meaning.
`prose` reports wording changes without awarding semantic correctness.

Record matching uses conservative docket normalization within each case, never
cross-case joining or list position. Duplicate identities and ambiguous null
dockets are explicit diagnostics. Missing/additional records and incomplete or
failed execution are reported separately from accuracy. Record-set completeness
controls whether additional output records have an adjudicated expected set.

The evaluation provides per-case deltas and `corrected`, `regressed`,
`unchanged_error`, `unchanged_correct`, `changed_unscored`, and
`unchanged_unscored` field classifications. The same paired comparable fields
form both accuracy denominators. Label coverage exposes how many eligible
labels were actually comparable; missing records and incompatible sources can
reduce coverage without silently becoming field errors. Accuracy is null with
no comparable labels. These are descriptive counts, not population estimates.

Generate the deterministic dashboard fixture with:

```sh
python appeal_demo.py /tmp/appeal-demo.json
```

The destination must not already exist. The `appeal-demo-1` envelope contains
baseline, candidate, reference, evaluation, frozen synthetic sources, and a
prominent software-test notice. Any cost, graph, or execution differences in the
fixture are simulated. They are not an implemented alternate strategy or actual
research outcomes. Future production selection is described separately in
[the methodology proposal](reference-selection-proposal.md).
