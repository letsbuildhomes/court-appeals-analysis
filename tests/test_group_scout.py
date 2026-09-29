"""Offline checks for the two-turn group scouting runner."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import duckdb

import group_scout
import groups


def member(sha, dockets, posted):
    basis, key = groups.classify(dockets, dockets)
    return {"sha": sha, "title": f"Ruling {sha}", "posted": posted, "listing_raw": None,
            "listing_dockets": dockets, "caption_dockets": dockets, "key_dockets": key,
            "basis": basis, "flags": [], "pages": 1, "chars": 40,
            "housing_terms": 0, "municipal_terms": 0}


TEXT = {"a": "Neighbors appeal the DRB approval of a duplex.",
        "b": "Judgment: the duplex is approved with conditions.",
        "c": "Act 250 permit for a gravel pit."}


def triage_json(documents, housing):
    return json.dumps({"schema_version": "v4-triage", "documents": [
        {"doc": d, "caption_dockets": [], "belongs": "yes"} for d in documents],
        "matter": "A matter.", "municipal_appeal": "yes" if housing else "no",
        "housing": "yes" if housing else "no", "reason": "Stated.",
        "evidence": [{"doc": 1, "page": 1, "quote": "duplex" if housing else "gravel pit"}]})


EXTRACT = json.dumps({"schema_version": "v4", "project": {
    "summary": "A duplex.", "municipality": "Town", "units": 2,
    "evidence": [{"doc": 1, "page": 1, "quote": "approval of a duplex"}]},
    "processes": [{"level": "environmental_division", "body": "Court", "events": [
        {"date": "2020-05", "kind": "ruling", "outcome": "approved_with_conditions", "final": True,
         "mechanism": "merits", "summary": "Approved.",
         "evidence": [{"doc": 2, "page": 1, "quote": "approved with conditions"}]}],
        "participants": [{"name": "Neighbors", "kind": "group", "roles": ["appellant"],
                          "relation": "neighbor", "evidence": [{"doc": 1, "page": 1, "quote": "not on page"}]}],
        "grounds": []}], "uncertainties": [], "suggestions": []})


def fake_completion(endpoint, key, payload, timeout):
    messages = payload["messages"]
    if len(messages) == 2:
        housing = "gravel" not in messages[1]["content"]
        docs = [1, 2] if 'doc="2"' in messages[1]["content"] else [1]
        content = triage_json(docs, housing)
    else:
        content = EXTRACT
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 900 if len(messages) > 2 else 0,
                           "completion_tokens": 50, "total_tokens": 1050,
                           "completion_tokens_details": {"reasoning_tokens": 20}}}, None, 0.1


class GroupScoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.groups_db, self.text_db, self.scout_db = (root / "groups.duckdb", root / "text.duckdb",
                                                       root / "scout.duckdb")
        groups.write_groups(groups.build_groups([
            member("a", ["1-1-20 Vtec"], "2020-02-01"), member("b", ["1-1-20 Vtec"], "2020-06-01"),
            member("c", ["5-5-20 Vtec"], "2020-07-01")]), self.groups_db)
        with duckdb.connect(str(self.text_db)) as conn:
            conn.execute("CREATE TABLE current_analysis_pages (pdf_sha256 VARCHAR, page_number INTEGER, status VARCHAR, source VARCHAR, text VARCHAR)")
            for sha, text in TEXT.items():
                conn.execute("INSERT INTO current_analysis_pages VALUES (?, 1, 'usable', 'native', ?)", [sha, text])
        for target, value in (("GROUPS_DB_PATH", self.groups_db), ("TEXT_DB_PATH", self.text_db),
                              ("SCOUT_DB_PATH", self.scout_db)):
            patcher = mock.patch.object(group_scout, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"SCOUT_API_BASE_URL": "http://127.0.0.1:8768",
                                             "SCOUT_MODEL": "m", "SCOUT_API_KEY": "via-proxy"})
        env.start()
        self.addCleanup(env.stop)

    def quiet(self, fn, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            fn(*args)
        return out.getvalue()

    def plan(self):
        args = SimpleNamespace(group=["g-1-1-20-vtec", "g-5-5-20-vtec"], note=["g-5-5-20-vtec=gravel"],
                               label="test", triage_reasoning="low", extract_reasoning="high",
                               max_chars=10_000, prompt_set="v4")
        with redirect_stdout(io.StringIO()):
            return group_scout.plan(args)

    def test_render_marks_membership_basis(self):
        with duckdb.connect(str(self.groups_db), read_only=True) as conn:
            group = group_scout.load_group(conn, "g-1-1-20-vtec")
        text, pages = group_scout.render_group(group["group_id"], group["documents"])
        self.assertIn('<document doc="1" basis="agree" caption_dockets="1-1-20 Vtec"', text)
        self.assertNotIn("Ruling a", text)  # listing titles are not model input
        self.assertEqual(pages[2][1], TEXT["b"])

    def test_triage_routes_and_extraction_continues_the_conversation(self):
        batch = self.plan()
        run_args = SimpleNamespace(batch_id=batch, workers=2, timeout=1, delay=0, retry_uncertain=False)
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion) as post:
            output = self.quiet(group_scout.run, run_args)
            self.assertEqual(post.call_count, 3)
            extract = next(c.args[2] for c in post.call_args_list if len(c.args[2]["messages"]) == 4)
            triage = next(c.args[2] for c in post.call_args_list
                          if len(c.args[2]["messages"]) == 2 and 'doc="2"' in c.args[2]["messages"][1]["content"])
            self.assertEqual(extract["messages"][:2], triage["messages"])
            self.assertEqual(extract["messages"][2]["role"], "assistant")
            self.assertEqual((triage["reasoning_effort"], extract["reasoning_effort"]), ("low", "high"))
            self.assertIn("out_of_scope 1", output)
            self.assertIn("extract 1", output)
            self.assertIn("cached 900", output)
            again = self.quiet(group_scout.run, run_args)
            self.assertEqual(post.call_count, 3)
            self.assertIn("0 requests, 3 cached turns", again)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            rows = conn.execute("""SELECT group_id, step, status, route, validation_warnings
                FROM group_turns ORDER BY attempted_at""").fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows[:3]].count(("g-5-5-20-vtec", "triage", "ok")), 1)
        extracted = next(r for r in rows if r[1] == "extract" and r[2] == "ok")
        self.assertIn("quote not found", extracted[4])
        self.assertEqual({r[2] for r in rows[3:]}, {"cached"})
        report = io.StringIO()
        with redirect_stdout(report):
            group_scout.report(SimpleNamespace(batch_id=batch))
        self.assertIn("triage: ok -> out_of_scope", report.getvalue())

    def test_oversized_groups_are_skipped_without_requests(self):
        args = SimpleNamespace(group=["g-1-1-20-vtec"], note=None, label=None, triage_reasoning=None,
                               extract_reasoning=None, max_chars=10, prompt_set="v4")
        with redirect_stdout(io.StringIO()):
            batch = group_scout.plan(args)
        with mock.patch.object(group_scout, "request_completion") as post:
            self.quiet(group_scout.run, SimpleNamespace(batch_id=batch, workers=1, timeout=1,
                                                        delay=0, retry_uncertain=False))
        post.assert_not_called()

    def test_from_batch_preserves_frozen_members_and_requeued_documents(self):
        original = self.plan()
        run_args = SimpleNamespace(batch_id=original, workers=1, timeout=1,
                                   delay=0, retry_uncertain=False)
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion):
            self.quiet(group_scout.run, run_args)
        with duckdb.connect(str(self.scout_db)) as conn:
            conn.execute("""UPDATE group_turns SET route_json = ?
                WHERE batch_id = ? AND group_id = 'g-1-1-20-vtec' AND step = 'triage'""",
                [json.dumps({"excluded_docs": [2]}), original])
        args = SimpleNamespace(group=[], note=None, label="requeue", triage_reasoning="low",
                               extract_reasoning="high", max_chars=10_000, prompt_set="v4.4",
                               from_batch=original, requeue_excluded=[original])
        with redirect_stdout(io.StringIO()):
            source = group_scout.plan(args)
        args.from_batch = source
        args.label = "clone"
        with mock.patch.object(group_scout, "load_group", side_effect=AssertionError("must use snapshot")), \
                redirect_stdout(io.StringIO()):
            clone = group_scout.plan(args)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            def members(batch):
                return conn.execute("""SELECT ordinal, group_id, dockets, composition,
                    documents_json, text_chars, note FROM group_batch_members
                    WHERE batch_id = ? ORDER BY ordinal""", [batch]).fetchall()
            self.assertEqual(members(clone), members(source))
            rows = members(clone)
        self.assertEqual(len(rows), 3)  # requeue option must not duplicate the frozen singleton
        self.assertEqual(rows[-1][1], "g-1-1-20-vtec~doc2")
        doc = json.loads(rows[-1][4])[0]
        self.assertEqual((doc["doc"], doc["sha"], doc["basis"]), (1, "b", "requeued"))
        self.assertEqual(doc["flags"], ["excluded_from:g-1-1-20-vtec"])
        self.assertEqual(rows[1][-1], "gravel")
        args.from_batch = clone
        args.only_group = ["g-1-1-20-vtec~doc2"]
        with mock.patch.object(group_scout, "load_group", side_effect=AssertionError("must use snapshot")), \
                redirect_stdout(io.StringIO()):
            selected = group_scout.plan(args)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            rows_selected = conn.execute("""SELECT group_id, documents_json FROM group_batch_members
                WHERE batch_id = ?""", [selected]).fetchall()
        self.assertEqual(rows_selected, [(rows[-1][1], rows[-1][4])])
        args.only_group = ["g-not-in-snapshot"]
        with self.assertRaisesRegex(ValueError, "--only-group"):
            self.quiet(group_scout.plan, args)

    def test_spend_limit_blocks_new_requests(self):
        batch = self.plan()
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion) as post:
            self.quiet(group_scout.run, SimpleNamespace(batch_id=batch, workers=1, timeout=1, delay=0,
                                                        retry_uncertain=False, max_spend=0.01,
                                                        spend_since=""))
        post.assert_not_called()

    def test_new_peak_pricing_does_not_reprice_historical_or_cached_turns(self):
        batch = self.plan()
        args = SimpleNamespace(batch_id=batch, workers=1, timeout=1, delay=0,
                               retry_uncertain=False, triage_only=True, max_output_tokens=100)
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion):
            self.quiet(group_scout.run, args)
        # Recreate historical rows written before multiplier metadata existed.
        with duckdb.connect(str(self.scout_db)) as conn:
            rows = conn.execute("SELECT turn_id, usage_json FROM group_turns").fetchall()
            for turn_id, raw in rows:
                usage = json.loads(raw)
                usage.pop("_price_multiplier", None)
                conn.execute("UPDATE group_turns SET usage_json = ? WHERE turn_id = ?",
                             [json.dumps(usage), turn_id])
            historical = group_scout.spent_usd(conn)
        # Triage fake responses have zero cached tokens, unlike extract responses.
        triage_base = (1000 * 0.15 + 50 * 0.6) / 1_000_000
        self.assertAlmostEqual(historical, 2 * triage_base)
        args.price_multiplier = 2
        args.max_output_tokens = 101  # changed request genuinely requires new calls
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion) as post:
            self.quiet(group_scout.run, args)
            self.assertEqual(post.call_count, 2)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            spent = group_scout.spent_usd(conn)
            usages = [json.loads(row[0]) for row in conn.execute(
                "SELECT usage_json FROM group_turns ORDER BY attempted_at").fetchall()]
        self.assertAlmostEqual(spent, historical + 4 * triage_base)
        self.assertEqual([u.get("_price_multiplier") for u in usages], [None, None, 2, 2])
        args.price_multiplier = 3  # cached results must retain old pricing and add no bill
        args.max_spend = spent
        with mock.patch.object(group_scout, "request_completion") as post:
            output = self.quiet(group_scout.run, args)
        post.assert_not_called()
        self.assertIn("0 requests, 2 cached turns", output)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            self.assertAlmostEqual(group_scout.spent_usd(conn), spent)

    def test_reservation_counts_in_flight_requests_before_launching_next(self):
        batch = self.plan()
        args = SimpleNamespace(batch_id=batch, workers=2, timeout=1, delay=0,
                               retry_uncertain=False, triage_only=True, max_output_tokens=100,
                               max_spend=0.015, price_multiplier=2)
        # Isolate scheduler accounting from token estimation: each request reserves a cent.
        with mock.patch.object(group_scout, "request_cost_bound", return_value=0.01), \
                mock.patch.object(group_scout, "request_completion", side_effect=fake_completion) as post:
            self.quiet(group_scout.run, args)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[2]["max_tokens"], 100)

    def test_unknown_usage_reserves_bound_and_stops_new_requests(self):
        for transport_uncertain in (False, True):
            with self.subTest(transport_uncertain=transport_uncertain):
                batch = self.plan()
                args = SimpleNamespace(batch_id=batch, workers=1, timeout=1, delay=0,
                                       retry_uncertain=False, max_output_tokens=100 + int(transport_uncertain),
                                       price_multiplier=2)
                def completion(endpoint, key, payload, timeout):
                    if transport_uncertain:
                        return 0, None, "Delivery uncertain after timeout", 0.1
                    status, response, error, elapsed = fake_completion(endpoint, key, payload, timeout)
                    response.pop("usage")
                    return status, response, error, elapsed
                with mock.patch.object(group_scout, "request_completion", side_effect=completion) as post:
                    if transport_uncertain:
                        with self.assertRaises(SystemExit):
                            self.quiet(group_scout.run, args)
                    else:
                        self.quiet(group_scout.run, args)
                self.assertEqual(post.call_count, 1)
                with duckdb.connect(str(self.scout_db), read_only=True) as conn:
                    raw_usage, raw_request = conn.execute("""SELECT usage_json, request_json
                        FROM group_turns WHERE batch_id = ?""", [batch]).fetchone()
                usage, request = json.loads(raw_usage), json.loads(raw_request)
                self.assertAlmostEqual(usage["_reserved_cost_usd"],
                                       group_scout.request_cost_bound(request, 2))
                self.assertEqual(usage["_price_multiplier"], 2)
                self.assertAlmostEqual(group_scout.cost_usd(usage), usage["_reserved_cost_usd"])

    def test_server_error_without_usage_reserves_possible_delivery_cost(self):
        batch = self.plan()
        args = SimpleNamespace(batch_id=batch, workers=1, timeout=1, delay=0,
                               retry_uncertain=False, max_output_tokens=100, price_multiplier=2)
        with mock.patch.object(group_scout, "request_completion",
                               return_value=(502, {"error": "Bad gateway"}, "HTTP 502 Bad gateway", 0.1)) as post:
            with self.assertRaises(SystemExit):
                self.quiet(group_scout.run, args)
        self.assertEqual(post.call_count, 1)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            status, usage_raw, request_raw = conn.execute("""SELECT status, usage_json, request_json
                FROM group_turns WHERE batch_id = ?""", [batch]).fetchone()
            spend = group_scout.spent_usd(conn)
        self.assertEqual(status, "uncertain_delivery")
        bound = group_scout.request_cost_bound(json.loads(request_raw), 2)
        self.assertAlmostEqual(json.loads(usage_raw)["_reserved_cost_usd"], bound)
        self.assertAlmostEqual(spend, bound)

    def test_verification_freezes_original_contract_candidate_and_provenance_offline(self):
        from notebooks.refinement import verify_group

        source = self.plan()
        args = SimpleNamespace(batch_id=source, workers=1, timeout=1, delay=0, retry_uncertain=False)
        with mock.patch.object(group_scout, "request_completion", side_effect=fake_completion):
            self.quiet(group_scout.run, args)
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            original = conn.execute("""SELECT system_prompt, triage_prompt, extract_prompt
                FROM group_batches WHERE batch_id = ?""", [source]).fetchone()
            turn_id, candidate = conn.execute("""SELECT turn_id, response_text FROM group_turns
                WHERE batch_id = ? AND group_id = 'g-1-1-20-vtec' AND step = 'extract'""",
                [source]).fetchone()
            documents = conn.execute("""SELECT documents_json FROM group_batch_members
                WHERE batch_id = ? AND group_id = 'g-1-1-20-vtec'""", [source]).fetchone()[0]
        changed_local_prompts = dict(zip(("system", "triage", "extract"),
                                        (value + "\nLOCAL EDIT" for value in original)))
        with mock.patch("sys.argv", ["verify_group.py", source, "g-1-1-20-vtec"]), \
                mock.patch.object(group_scout, "load_prompts", return_value=changed_local_prompts), \
                mock.patch.object(group_scout, "request_completion") as post:
            self.quiet(verify_group.main)
        post.assert_not_called()
        with duckdb.connect(str(self.scout_db), read_only=True) as conn:
            batch, system, triage, extract, prompt_hash = conn.execute("""SELECT batch_id,
                system_prompt, triage_prompt, extract_prompt, prompt_sha256 FROM group_batches
                WHERE batch_id != ?""", [source]).fetchone()
            members = conn.execute("""SELECT group_id, documents_json, note FROM group_batch_members
                WHERE batch_id = ?""", [batch]).fetchall()
            original_response = conn.execute("SELECT response_text FROM group_turns WHERE turn_id = ?",
                                             [turn_id]).fetchone()[0]
        self.assertEqual((system, triage), original[:2])
        self.assertEqual(extract, original[2] + verify_group.REVIEW.format(candidate=candidate))
        self.assertEqual(prompt_hash, group_scout.digest(group_scout.canonical_json(
            {"system": system, "triage": triage, "extract": extract})))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0][:2], ("g-1-1-20-vtec", documents))
        self.assertIn(turn_id, members[0][2])
        self.assertIn(source, members[0][2])
        self.assertEqual(original_response, candidate)

    def test_routing_prefers_extraction_unless_explicitly_out(self):
        route = group_scout.route_triage({"municipal_appeal": "unclear", "housing": "yes",
                                          "documents": [{"doc": 1, "belongs": "no"}, {"doc": 2, "belongs": "yes"}]}, [1, 2])
        self.assertEqual((route["route"], route["excluded_docs"]), ("extract", [1]))
        self.assertEqual(group_scout.route_triage({"municipal_appeal": "yes", "housing": "no"}, [1])["route"],
                         "out_of_scope")
        self.assertEqual(group_scout.route_triage({"municipal_appeal": "yes", "housing": "yes",
                                                   "documents": [{"doc": 1, "belongs": "no"}]}, [1])["route"],
                         "no_members")

    def test_quotes_match_despite_typographic_punctuation(self):
        warnings = []
        pages = {1: {1: "the DRB\u2019s decision \u201cwas\u201d AFFIRMED \u2014 in\nfull"}}
        group_scout.check_evidence("x", [{"doc": 1, "page": 1, "quote": 'DRB\'s decision "was" AFFIRMED - in full'}],
                                   pages, warnings)
        self.assertEqual(warnings, [])
        group_scout.check_evidence("x", [{"doc": 1, "page": 1, "quote": "twenty-five single-family"}],
                                   {1: {1: "twenty-\nfive single\u2010 family"}}, warnings)
        self.assertEqual(warnings, [])

    def test_quote_tolerance_and_citation_correction(self):
        pages = {1: {1: "In re Harrison Conditional Use Permit Docket No. 83-6-12 Vtec (Appeal from Berkshire DRB approval)",
                     2: "the court considers only the decision below, the record made before the",
                     3: "municipal panel. Appellant=s house was built."},
                 2: {1: "An unrelated ruling."}}
        cites = [{"doc": 1, "page": 1, "quote": "In re Harrison Conditional Use Permit (Appeal from Berkshire DRB approval)"},
                 {"doc": 1, "page": 2, "quote": "the record made before the municipal panel"},
                 {"doc": 1, "page": 1, "quote": "the court considers only ... the record made"},
                 {"doc": 2, "page": 1, "quote": "Appellant's house was built"},
                 {"doc": 1, "page": 1, "quote": "a sentence that appears nowhere in these documents"}]
        warnings = []
        group_scout.check_evidence("x", cites, pages, warnings)
        self.assertEqual((cites[3]["doc"], cites[3]["page"], cites[3]["cited_as"]), (1, 3, {"doc": 2, "page": 1}))
        self.assertEqual(cites[2]["page"], 2)
        self.assertEqual(len(warnings), 3, warnings)
        self.assertIn("quote not found", warnings[-1])
        bare = ["the record made before the municipal panel"]
        group_scout.check_evidence("y", bare, pages, warnings)
        self.assertEqual(bare[0], {"doc": 1, "page": 2, "quote": "the record made before the municipal panel"})

    def test_extract_validation_flags_enums_lots_and_excluded_docs(self):
        pages = {1: {1: "a duplex"}, 2: {1: "b"}}
        raw = json.dumps({"schema_version": "v4", "project": {"summary": "x", "units": "4",
                          "evidence": [{"doc": 2, "page": 1, "quote": "b"}]},
                          "processes": [{"level": "court", "body": "C", "events": [{"summary": "no kind",
                                         "evidence": [{"doc": 1, "page": 1, "quote": "duplex"}]}]}],
                          "uncertainties": [], "suggestions": []})
        parsed, warnings = group_scout.validate_extract(raw, pages, excluded={2})
        self.assertIsNotNone(parsed)
        text = "\n".join(warnings)
        for expected in ("units must be", "level 'court'", "missing kind", "triage excluded"):
            self.assertIn(expected, text)
        self.assertIsNone(group_scout.validate_extract('{"schema_version":"v3"}', pages)[0])


FLAT = {"schema_version": "v4.1", "project": {"summary": "A duplex.", "units": 2,
         "evidence": [{"doc": 1, "page": 1, "quote": "approval of a duplex"}]},
         "processes": [{"level": "municipal", "body": "DRB"},
                       {"level": "environmental_division", "body": "Court"}],
         "participants": [{"p": 2, "name": "Neighbors", "kind": "group", "roles": ["appellant"],
                           "relation": "neighbor", "evidence": [{"doc": 1, "page": 1, "quote": "Neighbors appeal"}]}],
         "grounds": [{"p": 2, "summary": "Too dense", "raised_by": "Neighbors", "topic": "density",
                      "category": "objective", "result": "failed",
                      "evidence": [{"doc": 1, "page": 1, "quote": "duplex"}]}],
         "remarks": [],
         "events": [{"p": 3, "kind": "ruling", "outcome": "approved_with_conditions", "summary": "Approved.",
                     "effects": [{"measure": "units", "change": "restored"}],
                     "evidence": [{"doc": 2, "page": 1, "quote": "approved with conditions"}]}],
         "uncertainties": [], "suggestions": []}


def focused_record():
    cite = [{"doc": 1, "page": 1, "quote": "Neighbors appeal the DRB approval of a duplex"}]
    return {"docket": "22-ENV-00124", "related_dockets": [],
            "project": {"description": "A duplex", "municipality": None,
                        "proposed_units": 2, "proposed_lots": None, "evidence": cite},
            "municipal_decision": {"description": "DRB approval", "date": None,
                                   "outcome": "approved", "evidence": cite},
            "appellants": [{"name": "Neighbors", "relationship": "neighbor",
                            "seeks": "overturn_approval", "representation": "unclear",
                            "evidence": cite}],
            "discretion": {"involved": "unclear", "description": None, "evidence": [],
                           "central": "unclear", "central_evidence": []},
            "timeline": [{"kind": "filing", "date": "2020-05", "court_or_body": "Court",
                          "dockets": ["22-ENV-00124"], "description": "Appeal filed",
                          "evidence": cite}],
            "observed_status": {"litigation": "unclear", "permission": "unclear",
                                "description": None, "evidence": []},
            "restrictions": [], "housing_changes": []}


class FocusedSchemaTests(unittest.TestCase):
    pages = {1: {1: TEXT["a"]}, 2: {1: TEXT["b"]}}

    def extract(self, record=None):
        return {"schema_version": "v5", "dockets": [record or focused_record()],
                "scouting": {"uncertainties": [], "suggestions": []}}

    def test_v6_direct_outcome_and_descriptive_changes(self):
        value = self.extract()
        value['schema_version'] = 'v6'
        record = value['dockets'][0]
        record['observed_status']['permission'] = 'allowed_with_conditions'
        record['housing_changes'] = [{'description': 'Design condition', 'evidence': []}]
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version='v6')
        self.assertIsNotNone(parsed)
        self.assertTrue(warnings)  # Evidence is still required for known claims.
        self.assertEqual(group_scout.extraction_counts(parsed)['dockets'], 1)
        record['housing_changes'][0]['units_before'] = 2
        parsed, errors = group_scout.validate_extract(json.dumps(value), self.pages, version='v6')
        self.assertIsNone(parsed)
        self.assertTrue(any('unexpected units_before' in e for e in errors))
        del record['housing_changes'][0]['units_before']
        record['observed_status']['permission'] = 'overturned'
        parsed, errors = group_scout.validate_extract(json.dumps(value), self.pages, version='v6')
        self.assertIsNone(parsed)
        self.assertTrue(any('permission' in e for e in errors))

    def test_v6_preserves_existing_triage_and_distinct_extraction_version(self):
        prompts = group_scout.load_prompts('v6')
        self.assertEqual(group_scout.schema_of(prompts['triage']), 'v5-triage')
        self.assertEqual(group_scout.schema_of(prompts['extract']), 'v6')

    def test_v5_prompt_and_triage_contract(self):
        prompts = group_scout.load_prompts("v5")
        self.assertEqual(group_scout.schema_of(prompts["triage"]), "v5-triage")
        self.assertEqual(group_scout.schema_of(prompts["extract"]), "v5")
        self.assertIn("Documents are evidence", prompts["system"])
        triage = {"schema_version": "v5-triage", "documents": [
            {"doc": 1, "caption_dockets": [], "belongs": "yes"},
            {"doc": 2, "caption_dockets": [], "belongs": "yes"}],
            "matter": "A duplex appeal", "municipal_appeal": "yes", "housing": "yes",
            "reason": "Housing decision appealed", "evidence": [
                {"doc": 1, "page": 1, "quote": "approval of a duplex"}]}
        parsed, warnings = group_scout.validate_triage(json.dumps(triage), self.pages, "v5-triage")
        self.assertEqual(parsed, triage)
        self.assertEqual(warnings, [])

    def test_v5_valid_record_and_counts(self):
        value = self.extract()
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version="v5")
        self.assertEqual(parsed, value)
        self.assertEqual(warnings, [])
        self.assertEqual(group_scout.extraction_counts(parsed),
                         {"dockets": 1, "related_dockets": 0, "appellants": 1,
                          "timeline": 1, "restrictions": 0, "housing_changes": 0})

    def test_v5_structure_errors_fail_without_rewriting_facts(self):
        mutations = [
            lambda x: x["dockets"][0].update(conduct_findings=[]),
            lambda x: x["dockets"][0]["project"].update(proposed_units="2"),
            lambda x: x["dockets"][0]["municipal_decision"].update(outcome="affirmed"),
            lambda x: x["dockets"][0]["timeline"][0].update(date="2020-13"),
            lambda x: x["dockets"][0]["appellants"][0].pop("seeks"),
            lambda x: x["dockets"][0]["project"].update(evidence=["bare quote"]),
            lambda x: x.update(uncertainties=[]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = self.extract()
                mutate(value)
                parsed, errors = group_scout.validate_extract(json.dumps(value), self.pages,
                                                                version="v5")
                self.assertIsNone(parsed)
                self.assertTrue(errors)

    def test_v5_scouting_is_optional_but_if_present_must_be_well_formed(self):
        value = self.extract()
        del value["scouting"]
        parsed, _ = group_scout.validate_extract(json.dumps(value), self.pages, version="v5")
        self.assertEqual(parsed, value)
        value["scouting"] = "not a feedback object"
        parsed, errors = group_scout.validate_extract(json.dumps(value), self.pages, version="v5")
        self.assertIsNone(parsed)
        self.assertTrue(errors)

    def test_v5_unknown_related_identifier_preserves_relationship(self):
        value = self.extract()
        value['dockets'][0]['related_dockets'] = [{'docket': None,
            'relationship': 'Supreme Court review, identifier unstated', 'evidence': []}]
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version='v5')
        self.assertEqual(parsed, value)
        self.assertTrue(any('related_dockets[0]: missing evidence' in w for w in warnings))

    def test_v5_normalizes_only_unrequested_null_metadata(self):
        value = self.extract()
        value['dockets'][0]['timeline'][0]['evidence'][0]['docket_note'] = None
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version='v5')
        self.assertNotIn('docket_note', parsed['dockets'][0]['timeline'][0]['evidence'][0])
        self.assertTrue(any('removed unrequested null field docket_note' in w for w in warnings))
        value['dockets'][0]['timeline'][0]['evidence'][0]['docket_note'] = 'substantive extra'
        parsed, _ = group_scout.validate_extract(json.dumps(value), self.pages, version='v5')
        self.assertIsNone(parsed)

    def test_v5_bracket_repair_keeps_nested_docket_attribution(self):
        value = self.extract()
        raw = json.dumps(value)
        broken = raw[:-1] + ']}'
        parsed, warnings = group_scout.validate_extract(broken, self.pages, version='v5')
        self.assertEqual(parsed, value)
        self.assertTrue(any('json repaired' in w for w in warnings))

    def test_v5_relocates_feedback_without_changing_research_record(self):
        value = self.extract()
        original = json.loads(json.dumps(value['dockets'][0]))
        value['dockets'][0]['scouting'] = {'uncertainties': ['Docket date unknown'], 'suggestions': []}
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version='v5')
        self.assertEqual(parsed['dockets'][0], original)
        self.assertIn('Docket date unknown', parsed['scouting']['uncertainties'])
        self.assertTrue(any('moved scouting feedback' in w for w in warnings))

    def test_v5_unknowns_need_no_evidence_and_known_claims_do(self):
        record = focused_record()
        record["docket"] = None
        record["project"] = {"description": None, "municipality": None,
                             "proposed_units": None, "proposed_lots": None, "evidence": []}
        record["municipal_decision"] = {"description": None, "date": None,
                                        "outcome": "unclear", "evidence": []}
        record["appellants"] = []
        record["timeline"] = []
        parsed, warnings = group_scout.validate_extract(json.dumps(self.extract(record)),
                                                          self.pages, version="v5")
        self.assertIsNotNone(parsed)
        self.assertEqual(warnings, [])
        record["discretion"]["involved"] = "yes"
        record["discretion"]["central"] = "yes"
        _, warnings = group_scout.validate_extract(json.dumps(self.extract(record)),
                                                    self.pages, version="v5")
        self.assertIn("dockets[0].discretion: missing evidence", warnings)
        self.assertIn("dockets[0].discretion.central: missing evidence", warnings)

    def test_v5_citations_warn_on_excluded_and_missing_quotes(self):
        record = focused_record()
        record["project"]["evidence"] = [{"doc": 2, "page": 1,
                                          "quote": "approved with conditions"}]
        record["timeline"][0]["evidence"] = [{"doc": 1, "page": 1,
                                               "quote": "not in document"}]
        _, warnings = group_scout.validate_extract(json.dumps(self.extract(record)), self.pages,
                                                    excluded={2}, version="v5")
        self.assertTrue(any("project.evidence[0]: cites doc 2" in w for w in warnings))
        self.assertTrue(any("timeline[0].evidence[0]: quote not found" in w for w in warnings))

    def test_v5_discretion_consistency_warns_without_repair(self):
        record = focused_record()
        record["discretion"].update(involved="no", central="yes")
        value = self.extract(record)
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages,
                                                          version="v5")
        self.assertEqual(parsed, value)
        self.assertTrue(any("central must be not_applicable" in w for w in warnings))
        self.assertTrue(any("discretion: missing evidence" in w for w in warnings))

    def test_v5_change_baseline_uses_proposal_not_existing_units(self):
        for measure in ("units", "lots"):
            with self.subTest(measure=measure):
                record = focused_record()
                record["project"][f"proposed_{measure}"] = 1
                change = dict(description="Approved proposal", units_before=None,
                              units_after=None, lots_before=None, lots_after=None,
                              evidence=record["project"]["evidence"])
                change.update({f"{measure}_before": 2, f"{measure}_after": 3})
                record["housing_changes"] = [change]
                value = self.extract(record)
                parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages,
                                                                 version="v5")
                self.assertEqual(parsed, value)
                self.assertTrue(any(f"review {measure} baseline" in w for w in warnings))
                # A genuine reduction from the proposed amount must not be flagged.
                record["project"][f"proposed_{measure}"] = 4
                change[f"{measure}_before"] = 4
                _, warnings = group_scout.validate_extract(json.dumps(self.extract(record)),
                                                           self.pages, version="v5")
                self.assertFalse(any("baseline" in w or "unchanged" in w for w in warnings))

    def test_v5_non_numeric_change_preserves_null_counts(self):
        record = focused_record()
        record["project"]["proposed_units"] = 1
        change = dict(description="Footprint limited", units_before=1, units_after=1,
                      lots_before=None, lots_after=None, evidence=record["project"]["evidence"])
        record["housing_changes"] = [change]
        value = self.extract(record)
        parsed, warnings = group_scout.validate_extract(json.dumps(value), self.pages, version="v5")
        self.assertEqual(parsed, value)
        self.assertTrue(any("unchanged units count" in w for w in warnings))
        change.update(units_before=None, units_after=None)
        _, warnings = group_scout.validate_extract(json.dumps(self.extract(record)), self.pages,
                                                   version="v5")
        self.assertFalse(any("baseline" in w or "unchanged" in w for w in warnings))


class FlatSchemaTests(unittest.TestCase):
    pages = {1: {1: TEXT["a"]}, 2: {1: TEXT["b"]}}

    def test_flat_extraction_checks_process_pointers(self):
        parsed, warnings = group_scout.validate_extract(json.dumps(FLAT), self.pages, version="v4.1")
        self.assertEqual(parsed["processes"][1]["level"], "environmental_division")
        self.assertEqual(warnings, ["events[0]: p must point to a process (1..2)"])
        self.assertEqual(group_scout.extraction_counts(parsed)["grounds"], 1)

    def test_v43_requires_docket_in_multi_docket_process(self):
        flat = json.loads(json.dumps(FLAT)); flat["schema_version"] = "v4.3"
        flat["processes"][1]["dockets"] = ["1-1-20 Vtec", "2-1-20 Vtec"]
        flat["events"][0]["p"] = 2
        _, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.3")
        self.assertIn("grounds[0]: missing docket for a multi-docket process", warnings)
        self.assertIn("events[0]: missing docket for a multi-docket process", warnings)

    def v44_multi_docket(self):
        flat = json.loads(json.dumps(FLAT))
        flat["schema_version"] = "v4.4"
        flat["processes"][1]["dockets"] = ["22-ENV-00124", "2-1-20 Vtec"]
        flat["events"][0].update(p=2, dockets=["22-ENV-124", "2-1-20 Vtec"])
        flat["grounds"][0]["dockets"] = ["22-ENV-00124"]
        return flat

    def test_v44_accepts_shared_docket_lists_and_normalized_aliases(self):
        flat = self.v44_multi_docket()
        parsed, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.4")
        self.assertIsNotNone(parsed)
        self.assertEqual(warnings, [])
        self.assertEqual(len(parsed["events"]), 1)  # one common ruling, not duplicate events

    def test_v44_flags_foreign_malformed_missing_and_legacy_attribution(self):
        for value, expected in [(["9-1-20 Vtec"], "not listed on its process"),
                                ([False], "array of strings"),
                                ("22-ENV-00124", "array of strings"),
                                ([], "missing dockets"),
                                (None, "missing dockets")]:
            with self.subTest(value=value):
                flat = self.v44_multi_docket()
                flat["events"][0]["dockets"] = value
                _, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.4")
                self.assertTrue(any(w.startswith("events[0]:") and expected in w for w in warnings), warnings)
        flat = self.v44_multi_docket()
        del flat["events"][0]["dockets"]
        flat["events"][0]["docket"] = "22-ENV-00124"
        _, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.4")
        self.assertIn("events[0]: use dockets array, not legacy docket", warnings)
        self.assertIn("events[0]: missing dockets for a multi-docket process", warnings)

    def test_v47_ruling_contract_warns_without_changing_extracted_facts(self):
        cases = [
            ({}, ["court ruling requires boolean final", "court ruling requires outcome"]),
            ({"final": "false", "outcome": "interim"}, ["final must be true or false"]),
            ({"final": False, "outcome": "mixed", "mechanism": "summary_judgment"},
             ["nonfinal ruling outcome must be interim or remanded"]),
            ({"final": False, "outcome": "interim", "mechanism": "summary_judgment"},
             ["interim ruling must omit mechanism"]),
            ({"final": True, "outcome": "interim"}, ["final ruling cannot have interim outcome"]),
            ({"final": False, "outcome": "interim"}, []),
            ({"final": False, "outcome": "remanded"}, []),
            ({"final": True, "outcome": "dismissed", "mechanism": "standing"}, []),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                flat = self.v44_multi_docket()
                flat["schema_version"] = "v4.7"
                event = flat["events"][0]
                event.pop("outcome")
                event.update(fields)
                parsed, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.7")
                self.assertEqual(warnings, ["events[0]: " + w for w in expected])
                self.assertEqual(parsed, flat)  # diagnostics must not repair semantic values

    def test_ruling_contract_preserves_prior_versions_and_non_ruling_events(self):
        for version in ("v4.4", "v4.5", "v4.6"):
            with self.subTest(version=version):
                flat = self.v44_multi_docket()
                flat["schema_version"] = version
                flat["events"][0].update(final=False, outcome="mixed", mechanism="summary_judgment")
                _, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version=version)
                self.assertEqual(warnings, [])
        for kind, outcome in (("decision", "approved"), ("stay", "granted")):
            with self.subTest(kind=kind):
                flat = self.v44_multi_docket()
                flat["schema_version"] = "v4.7"
                flat["events"][0].update(kind=kind, outcome=outcome)
                _, warnings = group_scout.validate_extract(json.dumps(flat), self.pages, version="v4.7")
                self.assertEqual(warnings, [])

    def test_bracket_slips_are_repaired_and_reported(self):
        text = json.dumps(FLAT)
        broken = text[:text.index(', "uncertainties"')].rstrip("]") + ', "uncertainties": [], "suggestions": []}'
        parsed, warnings = group_scout.validate_extract(broken, self.pages, version="v4.1")
        self.assertIsNotNone(parsed)
        self.assertTrue(warnings[0].startswith("json repaired:"))
        self.assertEqual(len(parsed["events"]), 1)

    def test_triage_v41_categories(self):
        raw = json.dumps({"schema_version": "v4.1-triage", "documents": [{"doc": 1, "belongs": "yes"},
                          {"doc": 2, "belongs": "yes"}], "matter": "m", "case_type": "zoning",
                          "development": "new_housing", "municipal_appeal": "yes", "housing": "yes",
                          "reason": "r", "evidence": [{"doc": 1, "page": 1, "quote": "duplex"}]})
        parsed, warnings = group_scout.validate_triage(raw, self.pages, "v4.1-triage")
        self.assertIsNotNone(parsed)
        self.assertEqual(warnings, ["triage: case_type 'zoning' is not an allowed value"])

    def test_cost_prices_hits_misses_and_output(self):
        usage = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 400_000,
                 "completion_tokens": 100_000, "total_tokens": 1_100_000}
        self.assertAlmostEqual(group_scout.cost_usd(usage), 0.4 * 0.003 + 0.6 * 0.15 + 0.1 * 0.6)
        self.assertAlmostEqual(group_scout.cost_usd({**usage, "_price_multiplier": 2}),
                               2 * group_scout.cost_usd(usage))
        self.assertEqual(group_scout.cost_usd({"_reserved_cost_usd": 0.1}), 0.1)

    def test_request_bound_includes_peak_input_and_capped_output(self):
        req = {"messages": [{"content": "abc"}], "max_tokens": 100}
        self.assertAlmostEqual(group_scout.request_cost_bound(req, 2),
                               2 * ((3 + 256 + 1024) * .15 + 100 * .6) / 1e6)


if __name__ == "__main__":
    unittest.main()
