"""Offline checks for the document scouting runner."""

import json
import io
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb
import httpx

import scout


class ScoutTests(unittest.TestCase):
    def test_usage_counts_include_provider_cache_fields(self):
        self.assertEqual(scout.usage_counts({
            "prompt_tokens": 100, "prompt_cache_hit_tokens": 40,
            "completion_tokens": 25, "total_tokens": 125,
        }), (100, 40, 25, 125))
        self.assertEqual(scout.usage_counts({
            "prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens": 25, "total_tokens": 125,
        }), (100, 30, 25, 125))
        self.assertIsNone(scout.usage_counts({"total_tokens": 125}))
        self.assertEqual(scout.reasoning_tokens({"completion_tokens_details": {
            "reasoning_tokens": 7}}), 7)

    def test_reasoning_matrix_runs_in_parallel_and_resumes_without_new_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "scout.duckdb"
            prompt = Path(tmp) / "scout_v3.md"
            prompt.write_text("Return V3 JSON.", encoding="utf-8")
            docs = [dict(sha=letter * 64, title=f"Ruling {letter}", date="2020-01-01",
                         pages=1, unresolved=0, chars=12) for letter in "ab"]
            cite = [{"page": 1, "quote": "Court order"}]
            parsed = {"schema_version": "v3", "document": {"kind": "order",
                      "summary": "Order", "evidence": cite}, "project": None,
                      "entities": [], "processes": [], "uncertainties": [], "suggestions": []}
            lock = threading.Lock()
            active = max_active = 0
            sent = []

            def respond(endpoint, key, payload, timeout):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    sent.append(payload)
                time.sleep(0.02)
                with lock:
                    active -= 1
                reasoning = payload["reasoning_effort"]
                return 200, {"choices": [{"message": {"content": json.dumps(parsed)},
                              "finish_reason": "stop"}], "usage": {
                                  "prompt_tokens": 10, "prompt_cache_hit_tokens": 2,
                                  "completion_tokens": 5,
                                  "completion_tokens_details": {"reasoning_tokens":
                                                                 0 if reasoning == "none" else 3},
                                  "total_tokens": 15}}, None, 0.02

            with mock.patch.object(scout, "SCOUT_DB_PATH", output), \
                 mock.patch.object(scout, "PROMPT_PATH", prompt), \
                 mock.patch.object(scout, "document_inventory", return_value=docs), \
                 mock.patch.object(scout, "render_document", return_value=("Court order", {1: "Court order"})), \
                 mock.patch.object(scout, "endpoint_from_env", return_value=(
                     "https://example.test/chat/completions", "test-model", "secret", "json_object")), \
                 mock.patch.object(scout, "request_completion", side_effect=respond):
                scout.plan(SimpleNamespace(limit=2, seed="pilot", order="random", sha=None,
                                                reasoning_levels="none,low,high"))
                with duckdb.connect(str(output), read_only=True) as conn:
                    batch_id = conn.execute("SELECT batch_id FROM batches").fetchone()[0]
                args = SimpleNamespace(batch_id=batch_id, timeout=1, delay=0,
                                       workers=2, retry_uncertain=False)
                scout.run(args)
                self.assertEqual(len(sent), 6)
                self.assertGreaterEqual(max_active, 2)
                self.assertEqual({p["reasoning_effort"] for p in sent},
                                 {"none", "low", "high"})
                self.assertTrue(all("thinking" not in p for p in sent))
                scout.run(args)
                self.assertEqual(len(sent), 6)
                result = io.StringIO()
                with redirect_stdout(result):
                    scout.compare(SimpleNamespace(batch_id=batch_id))
                self.assertIn("6", result.getvalue())
                self.assertIn("reasoning=0", result.getvalue())
                self.assertIn("reasoning=6", result.getvalue())

    def test_ambiguous_read_error_is_not_retried(self):
        calls = []

        def respond(request):
            calls.append(request)
            raise httpx.ReadTimeout("no response", request=request)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            status, response, error = scout.post_completion(
                client, "https://example.test/chat/completions", "secret", {"model": "x"}
            )
        self.assertEqual((status, response), (0, None))
        self.assertIn("Delivery uncertain", error)
        self.assertEqual(len(calls), 1)

    def test_render_document_preserves_page_boundaries_and_missing_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "text.duckdb"
            with duckdb.connect(str(db)) as conn:
                conn.execute("CREATE TABLE current_analysis_pages (pdf_sha256 VARCHAR, page_number INTEGER, status VARCHAR, source VARCHAR, text VARCHAR)")
                conn.execute("INSERT INTO current_analysis_pages VALUES ('abc', 1, 'usable', 'native', 'A < B'), ('abc', 2, 'unresolved', NULL, NULL)")
            with mock.patch.object(scout, "TEXT_DB_PATH", db):
                rendered, pages = scout.render_document("abc")
            self.assertIn('<page number="1" status="usable" source="native">', rendered)
            self.assertIn("A &lt; B", rendered)
            self.assertIn("[No selected text: unresolved]", rendered)
            self.assertEqual(pages, {1: "A < B", 2: ""})

    def test_shape_and_page_evidence_warnings(self):
        output = {"document": {"kind": "order", "finality": "interim",
                               "evidence": [{"page": 1, "quote": "Court order"}]},
                  **{field: [] for field in scout.REQUIRED_LISTS}, "uncertainties": []}
        output["events"] = [{"evidence": [{"page": 2, "quote": "missing"}]}]
        parsed, warnings = scout.validate_output(json.dumps(output), {1: "Court order"})
        self.assertIsNotNone(parsed)
        self.assertTrue(any("invalid page" in item for item in warnings))
        self.assertIsNone(scout.validate_output("not JSON", {1: ""})[0])

    def test_v1_requires_and_checks_appeal_posture(self):
        output = {"document": {"kind": "decision", "finality": "final_for_case",
                               "evidence": [{"page": 1, "quote": "permit denied"}]},
                  **{field: [] for field in scout.REQUIRED_LISTS}, "uncertainties": []}
        raw = json.dumps(output)
        self.assertIsNone(scout.validate_output(raw, {1: "permit denied"}, True)[0])
        output["appeal_postures"] = [{
            "track": "municipal", "decision_outcome": "denied",
            "appellant_relation": "applicant_or_developer",
            "posture": "applicant_challenge_to_denial",
            "evidence": [{"page": 1, "quote": "permit denied"}],
        }]
        parsed, warnings = scout.validate_output(
            json.dumps(output), {1: "permit denied"}, True)
        self.assertEqual(parsed["appeal_postures"][0]["posture"],
                         "applicant_challenge_to_denial")
        self.assertEqual(warnings, [])

    def test_v2_validates_nested_evidence_and_local_issue_links(self):
        cite = [{"page": 1, "quote": "duplex permit denied"}]
        output = {
            "schema_version": "v2", "document": {"kind": "order", "evidence": cite},
            "subject_project": {"use": "housing", "unit_count_basis": "building_type_conversion",
                                "proposed_housing_units": 2, "evidence": cite},
            **{field: [] for field in scout.V2_LISTS},
        }
        output["appeals"] = [{"appeal_id": "a1", "track": "municipal",
                              "posture": "applicant_challenge_to_denial",
                              "appealed_decision": {"outcome": "denied", "evidence": cite},
                              "evidence": cite}]
        output["issues"] = [{"issue_id": "i1", "topic": "conditional_use",
                             "appeal_id": "a1", "evidence": cite}]
        output["issue_actions"] = [{"issue_id": "i1", "action": "decided",
                                    "disposition_mechanism": "merits", "evidence": cite}]
        parsed, warnings = scout.validate_v2_output(
            json.dumps(output), {1: "duplex permit denied"})
        self.assertEqual(parsed["subject_project"]["proposed_housing_units"], 2)
        self.assertEqual(warnings, [])
        output["issue_actions"][0]["issue_id"] = "i9"
        _, warnings = scout.validate_v2_output(json.dumps(output), {1: "duplex permit denied"})
        self.assertIn("issue_actions[0]: unknown issue_id", warnings)
        del output["appeals"]
        self.assertIsNone(scout.validate_v2_output(json.dumps(output), {1: ""})[0])

    def test_v3_accepts_process_hierarchy_without_model_invented_ids(self):
        cite = [{"page": 1, "quote": "duplex permit denied"}]
        output = {
            "schema_version": "v3",
            "document": {"kind": "order", "summary": "Permit denial affirmed.", "evidence": cite},
            "project": {"description": "A duplex", "use": "housing", "municipality": "Sharon",
                        "proposed_units": 2, "evidence": cite},
            "entities": [{"name": "Applicant LLC", "kind": "business", "project_role": "developer",
                          "people": [{"name": "A. Attorney", "role": "attorney"}], "evidence": cite}],
            "processes": [
                {"kind": "municipal_panel", "authority": "Town DRB", "events": [
                    {"date": "2018-11-23", "kind": "decision", "effect": "end",
                     "summary": "Permit denied", "reported_as": "history", "outcome": "denied",
                     "evidence": cite}]},
                {"kind": "environmental_division", "authority": "Vermont Environmental Division",
                 "dockets": ["139-12-18 Vtec"], "appeals": [
                     {"track": "municipal", "decision": {"authority": "Town DRB",
                      "date": "2018-11-23", "outcome": "denied"},
                      "appellant": "Applicant LLC", "evidence": cite}],
                 "events": [{"date": "2021-04-06", "kind": "ruling", "effect": "end",
                             "summary": "Denial affirmed", "reported_as": "document_action",
                             "evidence": cite}],
                 "questions": [{"label": "Question 2", "topic": "zoning_standard",
                                "action": "decided", "answer": "No", "reason": "Not enforceable",
                                "mechanism": "summary_judgment", "evidence": cite}]},
            ],
            "uncertainties": [], "suggestions": [],
        }
        parsed, warnings = scout.validate_v3_output(json.dumps(output),
                                                     {1: "duplex permit denied"})
        self.assertEqual(parsed["processes"][1]["dockets"], ["139-12-18 Vtec"])
        self.assertEqual(warnings, [])
        output["processes"][1]["events"][0]["event_id"] = "e1"
        _, warnings = scout.validate_v3_output(json.dumps(output),
                                               {1: "duplex permit denied"})
        self.assertTrue(any("event_id is not a V3 field" in w for w in warnings))
        del output["processes"][1]["events"]
        self.assertIsNone(scout.validate_v3_output(json.dumps(output), {1: ""})[0])

    def test_existing_batches_keep_v0_prompt_after_migration(self):
        with duckdb.connect(":memory:") as conn:
            conn.execute("""CREATE TABLE batches (
                batch_id VARCHAR, created_at VARCHAR, seed VARCHAR,
                ordering VARCHAR, prompt_sha256 VARCHAR, document_count INTEGER)""")
            conn.execute("INSERT INTO batches VALUES ('old', '', '', '', '', 1)")
            scout.init_db(conn)
            prompt_path = conn.execute(
                "SELECT prompt_path FROM batches WHERE batch_id = 'old'"
            ).fetchone()[0]
        self.assertEqual(prompt_path, str(scout.LEGACY_PROMPT_PATH))

    def test_init_rekeys_endpoint_fingerprints_from_stored_request(self):
        with duckdb.connect(":memory:") as conn:
            scout.init_db(conn)
            request_json = scout.canonical_json({"model": "m", "messages": []})
            old_hash = scout.digest(scout.canonical_json({
                "endpoint": "https://example.test/chat/completions",
                "payload": json.loads(request_json)}))
            conn.execute("""INSERT INTO attempts (attempt_id, batch_id, pdf_sha256, attempted_at,
                request_sha256, input_sha256, prompt_sha256, model, endpoint, response_format,
                status, request_json) VALUES ('a', 'b', 'c', 'now', ?, 'i', 'p', 'm',
                'https://example.test/chat/completions', 'none', 'ok', ?)""",
                         [old_hash, request_json])
            scout.init_db(conn)
            self.assertEqual(conn.execute("SELECT request_sha256 FROM attempts").fetchone()[0],
                             scout.digest(request_json))

    def test_batch_runs_once_and_reuses_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, text, output, prompt = [root / name for name in
                                             ("archive.duckdb", "text.duckdb", "scout.duckdb", "prompt.md")]
            prompt.write_text("Return JSON.", encoding="utf-8")
            with duckdb.connect(str(archive)) as conn:
                conn.execute("CREATE TABLE snapshots (snapshot_id VARCHAR)")
                conn.execute("INSERT INTO snapshots VALUES ('latest')")
                conn.execute("CREATE TABLE listing_entries (snapshot_id VARCHAR, page_index INTEGER, ordinal INTEGER, media_url VARCHAR, title VARCHAR, date_attribute VARCHAR)")
                conn.execute("INSERT INTO listing_entries VALUES ('latest', 0, 1, 'url', 'Test PDF', '2020-01-01')")
                conn.execute("CREATE TABLE downloads (media_url VARCHAR, sha256 VARCHAR, status VARCHAR)")
                conn.execute("INSERT INTO downloads VALUES ('url', 'abc', 'downloaded')")
            with duckdb.connect(str(text)) as conn:
                conn.execute("CREATE TABLE documents (pdf_sha256 VARCHAR)")
                conn.execute("INSERT INTO documents VALUES ('abc')")
                conn.execute("CREATE TABLE current_analysis_pages (pdf_sha256 VARCHAR, page_number INTEGER, status VARCHAR, source VARCHAR, text VARCHAR)")
                conn.execute("INSERT INTO current_analysis_pages VALUES ('abc', 1, 'usable', 'native', 'Court order')")
            response = {"document": {"kind": "order", "finality": "interim",
                                     "evidence": [{"page": 1, "quote": "Court order"}]},
                        **{field: [] for field in scout.REQUIRED_LISTS}, "uncertainties": []}
            api_result = {"choices": [{"message": {"content": json.dumps(response)}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 2,
                                    "completion_tokens": 5, "total_tokens": 15}}
            patches = (mock.patch.object(scout, "DB_PATH", archive),
                       mock.patch.object(scout, "TEXT_DB_PATH", text),
                       mock.patch.object(scout, "SCOUT_DB_PATH", output),
                       mock.patch.object(scout, "PROMPT_PATH", prompt))
            with patches[0], patches[1], patches[2], patches[3]:
                scout.plan(SimpleNamespace(limit=1, seed="test", order="random"))
                with duckdb.connect(str(output), read_only=True) as conn:
                    batch_id, stored_prompt = conn.execute(
                        "SELECT batch_id, prompt_path FROM batches"
                    ).fetchone()
                    self.assertEqual(stored_prompt, str(prompt.resolve()))
                args = SimpleNamespace(batch_id=batch_id, timeout=1, delay=0,
                                       retry_uncertain=False)
                env = {"SCOUT_API_BASE_URL": "https://example.test/v1",
                       "SCOUT_MODEL": "test-model", "SCOUT_API_KEY": "never-store-this-key"}
                with mock.patch.dict("os.environ", env), mock.patch.object(
                    scout, "post_completion", return_value=(200, api_result, None)
                ) as post:
                    scout.run(args)
                    scout.run(args)
                    scout.plan(SimpleNamespace(limit=1, seed="another", order="random"))
                    with duckdb.connect(str(output), read_only=True) as conn:
                        second_batch = conn.execute(
                            "SELECT batch_id FROM batches WHERE batch_id <> ?", [batch_id]
                        ).fetchone()[0]
                    with mock.patch.dict("os.environ", {
                        "SCOUT_API_BASE_URL": "http://127.0.0.1:8768",
                        "SCOUT_API_KEY": "via-proxy",
                    }):
                        scout.run(SimpleNamespace(batch_id=second_batch, timeout=1,
                                                  delay=0, retry_uncertain=False))
                    scout.report(SimpleNamespace(batch_id=second_batch))
                    self.assertEqual(post.call_count, 1)
                    endpoint, key, payload = post.call_args.args[1:4]
                    self.assertEqual(endpoint, "https://example.test/v1/chat/completions")
                    self.assertEqual(key, "never-store-this-key")
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertNotIn("max_tokens", payload)
                    self.assertNotIn("thinking", payload)
                    self.assertIn('<page number="1"', payload["messages"][1]["content"])
                with duckdb.connect(str(output), read_only=True) as conn:
                    rows = conn.execute("SELECT status, request_json, parsed_json FROM attempts").fetchall()
                    paid_run = conn.execute(
                        "SELECT run_id FROM runs WHERE requested_count = 1"
                    ).fetchone()[0]
                    summary = scout.run_token_summary(conn, paid_run)
                self.assertEqual(len(rows), 2)
                self.assertEqual({row[0] for row in rows}, {"ok", "cached"})
                self.assertTrue(all("never-store-this-key" not in row[1] for row in rows))
                parsed = next(row[2] for row in rows if row[0] == "ok")
                self.assertEqual(json.loads(parsed)["document"]["evidence"][0]["page"], 1)
                self.assertIn("input 10", summary)
                self.assertIn("cached input 2", summary)
                self.assertIn("output 5", summary)
                self.assertIn("total 15", summary)


if __name__ == "__main__":
    unittest.main()
