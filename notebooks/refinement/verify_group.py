"""Freeze a single-group source-verification experiment; makes no API call.

Usage: verify_group.py SOURCE_BATCH GROUP_ID

The existing runner executes the returned batch with its usual cache, pricing,
and budget guard. The complete contract, candidate, and review instructions are
frozen in the extraction prompt. This is an experiment, not an automatic repair
or a replacement of the original record. No case-specific audit findings are
provided to the verifier.
"""
import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import duckdb
import group_scout as gs

REVIEW = """
## Independent source verification

The candidate below is untrusted model output, not evidence. Review it against
the original page-tagged documents and the contract above. Return a complete
corrected extraction JSON object in the same schema, not a critique or patch.

For each factual object, identify its referent in the source and check all of
its fields together. Verify that speaker, proposition, judicial response, and
result describe the same argument; that an event's outcome and finality describe
the same proceeding; and that quantities belong to the stated application.
Check the cited passage supports the interpretation, not just its vocabulary.
Check source chronology for superseded rulings and material omissions. Remove
unsupported facts; retain uncertainty instead of supplying plausible details.
Preserve correct information. Do not invent a correction just to change the draft.

<candidate>
{candidate}
</candidate>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch")
    parser.add_argument("group")
    args = parser.parse_args()
    with duckdb.connect(str(gs.SCOUT_DB_PATH), read_only=True) as conn:
        source = conn.execute("SELECT system_prompt, triage_prompt, extract_prompt, triage_reasoning, extract_reasoning, max_chars FROM group_batches WHERE batch_id=?", [args.batch]).fetchone()
        draft = conn.execute("SELECT turn_id, response_text FROM group_turns WHERE batch_id=? AND group_id=? AND step='extract' AND status IN ('ok','cached') ORDER BY attempted_at DESC LIMIT 1", [args.batch, args.group]).fetchone()
    if not source or not draft:
        raise SystemExit("A frozen source batch and successful extraction are required")
    version = gs.schema_of(source[2])
    batch = gs.plan(SimpleNamespace(group=[], note=[f"{args.group}=Source verification of turn {draft[0]} from {args.batch}; no audit hints"],
        from_batch=args.batch, only_group=[args.group], requeue_excluded=[],
        prompt_set=version, triage_reasoning=source[3], extract_reasoning=source[4],
        label="INCOMPLETE-verification-plan-do-not-run", max_chars=source[5]))
    prompts = {"system": source[0], "triage": source[1],
               "extract": source[2] + REVIEW.format(candidate=draft[1])}
    with duckdb.connect(str(gs.SCOUT_DB_PATH)) as conn:
        conn.execute("UPDATE group_batches SET label='V1-source-verification', system_prompt=?, triage_prompt=?, extract_prompt=?, prompt_sha256=? WHERE batch_id=?", [prompts["system"], prompts["triage"], prompts["extract"], gs.digest(gs.canonical_json(prompts)), batch])
    print(f"Verification batch: {batch}; preserved candidate turn {draft[0]}")


if __name__ == "__main__":
    main()
