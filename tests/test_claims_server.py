"""Data and input checks for the read-only claims explorer."""

import json
import unittest

import duckdb

import group_scout
from claims_server import (document_detail, document_rows, group_batch_detail, group_batches,
                           group_result, query_page, query_sha)


class ClaimsServerTests(unittest.TestCase):
    def test_inventory_uses_human_title_and_latest_success(self):
        sha = "a" * 64
        other = "b" * 64
        with duckdb.connect(":memory:") as archive, \
             duckdb.connect(":memory:") as text_db, \
             duckdb.connect(":memory:") as scout:
            archive.execute("CREATE TABLE snapshots (snapshot_id VARCHAR)")
            archive.execute("INSERT INTO snapshots VALUES ('latest')")
            archive.execute("""CREATE TABLE listing_entries (
                snapshot_id VARCHAR, page_index INTEGER, ordinal INTEGER,
                media_url VARCHAR, title VARCHAR, date_attribute VARCHAR)""")
            archive.execute("INSERT INTO listing_entries VALUES ('latest', 0, 1, 'u1', 'Named ruling', '2021-01-01')")
            archive.execute("INSERT INTO listing_entries VALUES ('latest', 0, 2, 'u2', 'Unscouted ruling', '2020-01-01')")
            archive.execute("CREATE TABLE downloads (media_url VARCHAR, sha256 VARCHAR, status VARCHAR)")
            archive.execute("INSERT INTO downloads VALUES ('u1', ?, 'downloaded'), ('u2', ?, 'downloaded')", [sha, other])
            text_db.execute("CREATE TABLE documents (pdf_sha256 VARCHAR, page_count INTEGER, status VARCHAR)")
            text_db.execute("INSERT INTO documents VALUES (?, 3, 'ready'), (?, 2, 'ready')", [sha, other])
            scout.execute("""CREATE TABLE attempts (
                pdf_sha256 VARCHAR, attempt_id VARCHAR, batch_id VARCHAR,
                attempted_at VARCHAR, model VARCHAR, parsed_json VARCHAR,
                validation_warnings VARCHAR, usage_json VARCHAR,
                status VARCHAR, error VARCHAR)""")
            scout.execute("CREATE TABLE batches (batch_id VARCHAR, prompt_path VARCHAR)")
            scout.execute("INSERT INTO batches VALUES ('batch1', 'prompts/scout_v0.md'), ('batch2', 'prompts/scout_v1.md'), ('batch3', 'prompts/scout_v2.md'), ('batch4', 'prompts/scout_v3.md')")
            claims = {"document": {"summary": "A ruling"}, "events": [{"kind": "court_ruling"}],
                      "appeal_postures": [{"posture": "applicant_challenge_to_denial"}]}
            scout.execute("INSERT INTO attempts VALUES (?, 'attempt1', 'batch1', '2026-01-01', 'test-model', ?, '[]', NULL, 'ok', NULL)",
                          [sha, json.dumps(claims)])
            scout.execute("INSERT INTO attempts VALUES (?, 'attempt2', 'batch2', '2026-01-02', 'test-model', ?, '[\"citation warning\"]', NULL, 'ok', NULL)",
                          [sha, json.dumps(claims)])
            v2_claims = {"schema_version": "v2", "document": {"summary": "A revised ruling"},
                         "subject_project": {"description": "Duplex"},
                         "appeals": [{"track": "municipal"}], "issues": []}
            scout.execute("INSERT INTO attempts VALUES (?, 'attempt3', 'batch3', '2026-01-03', 'test-model', ?, '[]', NULL, 'ok', NULL)",
                          [sha, json.dumps(v2_claims)])
            v3_claims = {"schema_version": "v3", "document": {"summary": "A short ruling"},
                         "project": {"description": "Duplex"}, "entities": [{"name": "Developer"}],
                         "processes": [{"kind": "municipal_panel", "events": [{}]},
                                       {"kind": "environmental_division", "appeals": [{}],
                                        "events": [{}, {}], "questions": [{}, {}]}]}
            scout.execute("INSERT INTO attempts VALUES (?, 'attempt4', 'batch4', '2026-01-04', 'test-model', ?, '[]', NULL, 'ok', NULL)",
                          [sha, json.dumps(v3_claims)])
            scout.execute("ALTER TABLE attempts ADD COLUMN request_json VARCHAR DEFAULT '{}'")
            scout.execute("UPDATE attempts SET request_json = ?, usage_json = ? WHERE attempt_id = 'attempt4'",
                          [json.dumps({"reasoning_effort": "low"}),
                           json.dumps({"prompt_tokens": 10, "completion_tokens": 5,
                                       "completion_tokens_details": {"reasoning_tokens": 3},
                                       "total_tokens": 15})])
            rows = document_rows(text_db, archive, scout)
            self.assertEqual([(r["title"], r["status"]) for r in rows],
                             [("Named ruling", "extracted"), ("Unscouted ruling", "not_scouted")])
            self.assertEqual(rows[0]["feature_count"], 2)
            self.assertEqual(rows[0]["warning_count"], 1)
            detail = document_detail(text_db, archive, scout, sha)
            self.assertEqual(detail["attempt_id"], "attempt2")
            self.assertEqual(detail["claims"]["appeal_postures"][0]["posture"],
                             "applicant_challenge_to_denial")
            self.assertEqual(len(detail["attempts"]), 2)
            old = document_detail(text_db, archive, scout, sha, "v1", "attempt1")
            self.assertEqual(old["attempt_id"], "attempt1")
            v2_rows = document_rows(text_db, archive, scout, "v2")
            self.assertEqual(v2_rows[0]["feature_counts"]["subject_project"], 1)
            self.assertEqual(document_detail(text_db, archive, scout, sha, "v2")["attempt_id"],
                             "attempt3")
            with self.assertRaises(ValueError):
                document_detail(text_db, archive, scout, sha, "v2", "attempt2")
            v3_rows = document_rows(text_db, archive, scout, "v3")
            self.assertEqual(v3_rows[0]["feature_counts"]["processes"], 2)
            self.assertEqual(v3_rows[0]["feature_counts"]["events"], 3)
            self.assertEqual(v3_rows[0]["feature_counts"]["questions"], 2)
            v3_detail = document_detail(text_db, archive, scout, sha, "v3")
            self.assertEqual(v3_detail["attempt_id"], "attempt4")
            self.assertEqual(v3_detail["attempts"][0]["reasoning"], "low")
            self.assertEqual(v3_detail["attempts"][0]["total_tokens"], 15)
            self.assertEqual(v3_detail["attempts"][0]["reasoning_tokens"], 3)

    def test_group_run_views_separate_new_and_reused_tokens(self):
        with duckdb.connect(":memory:") as scout:
            self.assertEqual(group_batches(scout), [])
            group_scout.init_db(scout)
            scout.execute("""INSERT INTO group_batches VALUES ('b1', '2026-01-01', 'pilot', 'g1', 'p',
                's', 't', 'e', 'low', 'high', 1000, 1)""")
            documents = [{"doc": 1, "sha": "a" * 64, "basis": "disagree", "flags": [], "pages": 2}]
            scout.execute("INSERT INTO group_batch_members VALUES ('b1', 1, 'g-1', ['1-1-20 Vtec'], 'single', ?, 500, 'why')",
                          [json.dumps(documents)])
            usage = json.dumps({"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "completion_tokens": 10,
                                "total_tokens": 110, "completion_tokens_details": {"reasoning_tokens": 4}})
            columns = """(turn_id, batch_id, group_id, step, run_id, attempted_at, request_sha256,
                input_sha256, model, endpoint, reasoning, status, request_json, parsed_json,
                validation_warnings, usage_json, route, route_json)"""
            scout.execute(f"INSERT INTO group_turns {columns} VALUES ('t1', 'b1', 'g-1', 'triage', 'r1', '2026-01-01T00:00:01', 'h1', 'i', 'm', 'e', 'low', 'ok', '{{}}', ?, '[]', ?, 'extract', ?)",
                          [json.dumps({"matter": "A duplex."}), usage, json.dumps({"route": "extract", "excluded_docs": []})])
            scout.execute(f"INSERT INTO group_turns {columns} VALUES ('t2', 'b1', 'g-1', 'extract', 'r2', '2026-01-01T00:00:02', 'h2', 'i', 'm', 'e', 'high', 'cached', '{{}}', ?, '[\"w\"]', ?, NULL, NULL)",
                          [json.dumps({"processes": [{"events": [{}, {}], "grounds": [{}]}]}), usage])
            [batch] = group_batches(scout)
            self.assertEqual((batch["routes"], batch["extracted"], batch["usage"]["requests"]), ({"extract": 1}, 1, 1))
            detail = group_batch_detail(scout, "b1")
            group = detail["groups"][0]
            self.assertEqual(group["weak_documents"], 1)
            self.assertEqual(group["steps"]["triage"]["matter"], "A duplex.")
            self.assertEqual(group["steps"]["extract"]["counts"]["events"], 2)
            self.assertEqual(detail["usage"]["extract"]["requests"], 0)
            self.assertEqual(detail["usage"]["triage"]["cached"], 80)
            result = group_result(scout, "b1", "g-1")
            self.assertEqual([t["step"] for t in result["turns"]], ["triage", "extract"])
            self.assertEqual(result["turns"][1]["warnings"], ["w"])
            with self.assertRaises(LookupError):
                group_result(scout, "b1", "g-2")

    def test_source_parameters_are_strict(self):
        sha = "a" * 64
        self.assertEqual(query_sha({"sha": [sha]}), sha)
        self.assertEqual(query_page({"sha": [sha], "page": ["3"]}), (sha, 3))
        for query in ({"sha": ["../data"]}, {"sha": [sha], "page": ["0"]},
                      {"sha": [sha], "page": ["x"]}):
            with self.assertRaises(ValueError):
                query_page(query)


if __name__ == "__main__":
    unittest.main()
