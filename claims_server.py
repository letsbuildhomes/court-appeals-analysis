"""Read-only local explorer for PDF-level scouting claims and their evidence."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from contextlib import ExitStack
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from config import DB_PATH, ROOT, TEXT_DB_PATH
from listen import DEFAULT_HOST, display_url
from group_scout import cost_usd, extraction_counts
from review_server import source_pdf
from scout import SCOUT_DB_PATH, reasoning_from_request, reasoning_tokens, usage_counts


HTML_PATH = ROOT / "claims.html"
RUNS_HTML_PATH = ROOT / "runs.html"
SHA_RE = re.compile(r"[0-9a-f]{64}")
FEATURE_FIELDS = {
    "v1": ("appeal_postures", "docket_references", "project_mentions", "participants",
           "issues", "events", "case_relationships", "additional_observations"),
    "v2": ("docket_references", "appeals", "participants", "issues", "issue_actions",
           "events", "case_relationships", "schema_suggestions"),
    "v3": ("entities", "processes", "appeals", "events", "questions", "links"),
}


def query_schema(query: dict[str, list[str]]) -> str:
    schema = query.get("schema", ["v1"])[0]
    if schema not in FEATURE_FIELDS:
        raise ValueError("Expected schema v1, v2, or v3")
    return schema


def schema_for_prompt(prompt_path: str | None) -> str:
    name = Path(prompt_path or "scout_v0.md").name
    if name == "scout_v3.md":
        return "v3"
    return "v2" if name == "scout_v2.md" else "v1"


def listing_labels(archive: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, str | None]]:
    snapshot = archive.execute("SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
    if snapshot is None:
        return {}
    rows = archive.execute("""
        SELECT d.sha256, e.title, e.date_attribute
        FROM listing_entries e JOIN downloads d ON e.media_url = d.media_url
        WHERE e.snapshot_id = ? AND d.status = 'downloaded' AND d.sha256 IS NOT NULL
        ORDER BY e.page_index, e.ordinal
    """, [snapshot[0]]).fetchall()
    labels = {}
    for sha, title, date in rows:
        labels.setdefault(sha, (title, date))
    return labels


def versioned_attempts(scout: duckdb.DuckDBPyConnection | None, schema: str) -> tuple[dict, dict, dict]:
    if scout is None:
        return {}, {}, {}
    has_batches = scout.execute("""
        SELECT count(*) FROM information_schema.tables WHERE table_name = 'batches'
    """).fetchone()[0] > 0
    has_prompt_path = has_batches and "prompt_path" in {
        row[0] for row in scout.execute("DESCRIBE batches").fetchall()
    }
    join = "LEFT JOIN batches b ON a.batch_id = b.batch_id" if has_prompt_path else ""
    prompt = "b.prompt_path" if has_prompt_path else "NULL"
    request = ("a.request_json" if "request_json" in {
        row[0] for row in scout.execute("DESCRIBE attempts").fetchall()
    } else "'{}'")
    rows = scout.execute(f"""
        SELECT a.pdf_sha256, a.attempt_id, a.batch_id, a.attempted_at, a.model,
               a.parsed_json, a.validation_warnings, a.usage_json,
               a.status, a.error, {prompt}, {request}
        FROM attempts a {join}
        WHERE a.status <> 'cached'
        ORDER BY a.attempted_at DESC, a.attempt_id DESC
    """).fetchall()
    successes, failures, history = {}, {}, {}
    for row in rows:
        if schema_for_prompt(row[10]) != schema:
            continue
        sha = row[0]
        if row[8] == "ok" and row[5] is not None:
            successes.setdefault(sha, row)
            usage = json.loads(row[7]) if row[7] else None
            counts = usage_counts(usage)
            history.setdefault(sha, []).append({
                "attempt_id": row[1], "batch_id": row[2], "attempted_at": row[3],
                "model": row[4], "prompt": Path(row[10] or "scout_v0.md").name,
                "reasoning": reasoning_from_request(row[11]),
                "total_tokens": counts[3] if counts else None,
                "reasoning_tokens": reasoning_tokens(usage),
            })
        elif row[8] != "ok":
            failures.setdefault(sha, row)
    return successes, failures, history


def document_rows(text_db: duckdb.DuckDBPyConnection,
                  archive: duckdb.DuckDBPyConnection,
                  scout: duckdb.DuckDBPyConnection | None,
                  schema: str = "v1") -> list[dict]:
    labels = listing_labels(archive)
    successes, failures, _ = versioned_attempts(scout, schema)
    inventory = text_db.execute("""
        SELECT pdf_sha256, page_count FROM documents
        WHERE status = 'ready' ORDER BY pdf_sha256
    """).fetchall()
    documents = []
    for sha, page_count in inventory:
        title, posted = labels.get(sha, (f"Court PDF {sha[:12]}", None))
        extracted = successes.get(sha)
        parsed = json.loads(extracted[5]) if extracted else None
        warnings = json.loads(extracted[6] or "[]") if extracted else []
        if parsed and schema == "v3":
            processes = parsed.get("processes", [])
            counts = {
                "project": int(isinstance(parsed.get("project"), dict)),
                "entities": len(parsed.get("entities", [])),
                "processes": len(processes),
                **{field: sum(len(p.get(field, [])) for p in processes)
                   for field in ("appeals", "events", "questions", "links")},
            }
        else:
            counts = {field: len(parsed.get(field, [])) for field in FEATURE_FIELDS[schema]} if parsed else {}
            if parsed and schema == "v2" and isinstance(parsed.get("subject_project"), dict):
                counts["subject_project"] = 1
        status = "extracted" if extracted else "failed" if sha in failures else "not_scouted"
        documents.append({
            "sha": sha, "title": title, "posted_date": posted,
            "page_count": page_count, "status": status,
            "feature_count": sum(counts.values()), "feature_counts": counts,
            "warning_count": len(warnings),
            "last_attempted_at": extracted[3] if extracted else failures[sha][3] if sha in failures else None,
        })
    documents.sort(key=lambda item: item["posted_date"] or "", reverse=True)
    priority = {"extracted": 0, "failed": 1, "not_scouted": 2}
    documents.sort(key=lambda item: priority[item["status"]])
    return documents


def document_detail(text_db: duckdb.DuckDBPyConnection,
                    archive: duckdb.DuckDBPyConnection,
                    scout: duckdb.DuckDBPyConnection | None,
                    sha: str, schema: str = "v1", attempt_id: str | None = None) -> dict:
    row = text_db.execute("""
        SELECT page_count FROM documents WHERE pdf_sha256 = ? AND status = 'ready'
    """, [sha]).fetchone()
    if row is None:
        raise LookupError("PDF is not in the text inventory")
    title, posted = listing_labels(archive).get(sha, (f"Court PDF {sha[:12]}", None))
    successes, failures, history = versioned_attempts(scout, schema)
    extracted = successes.get(sha)
    if attempt_id:
        if attempt_id not in {a["attempt_id"] for a in history.get(sha, [])}:
            raise ValueError("Attempt is not a successful extraction for this PDF and schema")
        extracted = scout.execute("""
            SELECT pdf_sha256, attempt_id, batch_id, attempted_at, model,
                   parsed_json, validation_warnings, usage_json
            FROM attempts WHERE attempt_id = ?
        """, [attempt_id]).fetchone()
    latest_failure = failures.get(sha)
    return {
        "sha": sha, "schema": schema, "title": title, "posted_date": posted, "page_count": row[0],
        "status": "extracted" if extracted else "failed" if latest_failure else "not_scouted",
        "attempt_id": extracted[1] if extracted else None,
        "batch_id": extracted[2] if extracted else None,
        "attempted_at": extracted[3] if extracted else None,
        "model": extracted[4] if extracted else None,
        "attempts": history.get(sha, []),
        "claims": json.loads(extracted[5]) if extracted else None,
        "warnings": json.loads(extracted[6] or "[]") if extracted else [],
        "usage": json.loads(extracted[7]) if extracted and extracted[7] else None,
        "latest_failure": ({"status": latest_failure[8], "error": latest_failure[9],
                            "attempted_at": latest_failure[3]} if latest_failure else None),
    }


def has_group_tables(scout: duckdb.DuckDBPyConnection | None) -> bool:
    return scout is not None and scout.execute("""
        SELECT count(*) FROM information_schema.tables WHERE table_name = 'group_turns'
    """).fetchone()[0] > 0


def usage_fields(usage_json: str | None) -> dict | None:
    usage = json.loads(usage_json) if usage_json else None
    counts = usage_counts(usage)
    if counts is None:
        return None
    return {"input": counts[0], "cached": counts[1], "output": counts[2], "total": counts[3],
            "reasoning": reasoning_tokens(usage), "usd": cost_usd(usage)}


def latest_group_turns(scout: duckdb.DuckDBPyConnection, batch_id: str) -> dict[tuple[str, str], dict]:
    rows = scout.execute("""
        SELECT group_id, step, status, route, route_json, usage_json, validation_warnings,
               parsed_json, error, attempted_at
        FROM group_turns WHERE batch_id = ? ORDER BY attempted_at, turn_id
    """, [batch_id]).fetchall()
    latest = {}
    for row in rows:
        previous = latest.get((row[0], row[1]))
        if row[2] == "cached" and previous and previous["status"] == "ok":
            continue  # Reuse of an earlier success: keep showing the original turn.
        parsed = json.loads(row[7]) if row[7] else None
        latest[(row[0], row[1])] = {
            "status": row[2], "route": row[3], "route_detail": json.loads(row[4]) if row[4] else None,
            "usage": usage_fields(row[5]), "warning_count": len(json.loads(row[6] or "[]")),
            "parsed": parsed, "error": row[8], "attempted_at": row[9]}
    return latest


def fresh_usage(scout: duckdb.DuckDBPyConnection, where: str, params: list) -> dict:
    """Token sums for turns that made a request; cached reuse is excluded."""
    totals = {"requests": 0, "input": 0, "cached": 0, "output": 0, "reasoning": 0, "usd": 0.0}
    for (usage_json,) in scout.execute(f"""
        SELECT usage_json FROM group_turns
        WHERE {where} AND status NOT IN ('cached', 'skipped')
    """, params).fetchall():
        totals["requests"] += 1
        usage = usage_fields(usage_json)
        if usage:
            for key in ("input", "cached", "output", "reasoning", "usd"):
                totals[key] += usage[key] or 0
    return totals


def group_batches(scout: duckdb.DuckDBPyConnection | None) -> list[dict]:
    if not has_group_tables(scout):
        return []
    batches = []
    for batch_id, created, label, count, tr, er in scout.execute("""
        SELECT batch_id, created_at, label, group_count, triage_reasoning, extract_reasoning
        FROM group_batches ORDER BY created_at DESC
    """).fetchall():
        latest = latest_group_turns(scout, batch_id)
        routes: dict[str, int] = {}
        for (_, step), turn in latest.items():
            if step == "triage" and turn["route"]:
                routes[turn["route"]] = routes.get(turn["route"], 0) + 1
        batches.append({
            "batch_id": batch_id, "created_at": created, "label": label, "group_count": count,
            "reasoning": {"triage": tr, "extract": er}, "routes": routes,
            "extracted": sum(1 for (_, step), t in latest.items()
                             if step == "extract" and t["status"] in {"ok", "cached"}),
            "usage": fresh_usage(scout, "batch_id = ?", [batch_id]),
        })
    return batches


def group_batch_detail(scout: duckdb.DuckDBPyConnection | None, batch_id: str) -> dict:
    if not has_group_tables(scout):
        raise LookupError("No group batches yet")
    batch = scout.execute("""
        SELECT batch_id, created_at, label, grouping_version, triage_reasoning,
               extract_reasoning, max_chars, group_count
        FROM group_batches WHERE batch_id = ?
    """, [batch_id]).fetchone()
    if batch is None:
        raise LookupError("Unknown group batch")
    latest = latest_group_turns(scout, batch_id)
    groups = []
    for ordinal, group_id, dockets, composition, documents_json, chars, note in scout.execute("""
        SELECT ordinal, group_id, dockets, composition, documents_json, text_chars, note
        FROM group_batch_members WHERE batch_id = ? ORDER BY ordinal
    """, [batch_id]).fetchall():
        documents = json.loads(documents_json)
        steps = {}
        for step in ("triage", "extract"):
            turn = latest.get((group_id, step))
            if turn:
                parsed = turn.pop("parsed") or {}
                if step == "triage":
                    turn["matter"] = parsed.get("matter")
                else:
                    turn["counts"] = extraction_counts(parsed)
            steps[step] = turn
        groups.append({"ordinal": ordinal, "group_id": group_id, "dockets": list(dockets),
                       "composition": composition, "doc_count": len(documents),
                       "weak_documents": sum(1 for d in documents if d["basis"] in
                                             {"disagree", "listing_only", "none"} or d["flags"]),
                       "text_chars": chars, "note": note, "steps": steps})
    runs = []
    for run_id, started, finished, model, endpoint, requested, cached, failed in scout.execute("""
        SELECT run_id, started_at, finished_at, model, endpoint, requested_count,
               cached_count, failed_count FROM group_runs WHERE batch_id = ? ORDER BY started_at
    """, [batch_id]).fetchall():
        runs.append({"run_id": run_id, "started_at": started, "finished_at": finished,
                     "model": model, "endpoint": endpoint, "requested": requested,
                     "cached": cached, "failed": failed,
                     "steps": {step: fresh_usage(scout, "run_id = ? AND step = ?", [run_id, step])
                               for step in ("triage", "extract")}})
    return {"batch_id": batch[0], "created_at": batch[1], "label": batch[2],
            "grouping_version": batch[3], "reasoning": {"triage": batch[4], "extract": batch[5]},
            "max_chars": batch[6], "group_count": batch[7], "groups": groups, "runs": runs,
            "usage": {step: fresh_usage(scout, "batch_id = ? AND step = ?", [batch_id, step])
                      for step in ("triage", "extract")}}


def group_result(scout: duckdb.DuckDBPyConnection | None, batch_id: str, group_id: str) -> dict:
    if not has_group_tables(scout):
        raise LookupError("No group batches yet")
    member = scout.execute("""
        SELECT ordinal, dockets, composition, documents_json, text_chars, note
        FROM group_batch_members WHERE batch_id = ? AND group_id = ?
    """, [batch_id, group_id]).fetchone()
    if member is None:
        raise LookupError("Group is not in this batch")
    turns = []
    for row in scout.execute("""
        SELECT turn_id, step, status, attempted_at, run_id, model, reasoning, http_status,
               usage_json, parsed_json, validation_warnings, response_text, error, route_json,
               reused_turn_id, finish_reason, elapsed_seconds, length(request_json)
        FROM group_turns WHERE batch_id = ? AND group_id = ? ORDER BY attempted_at, turn_id
    """, [batch_id, group_id]).fetchall():
        turns.append({"turn_id": row[0], "step": row[1], "status": row[2], "attempted_at": row[3],
                      "run_id": row[4], "model": row[5], "reasoning": row[6], "http_status": row[7],
                      "usage": usage_fields(row[8]), "parsed": json.loads(row[9]) if row[9] else None,
                      "warnings": json.loads(row[10] or "[]"), "response_text": row[11],
                      "error": row[12], "route": json.loads(row[13]) if row[13] else None,
                      "reused_turn_id": row[14], "finish_reason": row[15], "elapsed": row[16],
                      "request_chars": row[17]})
    return {"batch_id": batch_id, "group_id": group_id, "ordinal": member[0],
            "dockets": list(member[1]), "composition": member[2],
            "documents": json.loads(member[3]), "text_chars": member[4], "note": member[5],
            "turns": turns}


def query_sha(query: dict[str, list[str]]) -> str:
    sha = query.get("sha", [""])[0]
    if not SHA_RE.fullmatch(sha):
        raise ValueError("Expected a PDF SHA-256")
    return sha


def query_page(query: dict[str, list[str]]) -> tuple[str, int]:
    sha = query_sha(query)
    page_raw = query.get("page", [""])[0]
    if not page_raw.isdecimal() or int(page_raw) < 1:
        raise ValueError("Expected a positive page number")
    return sha, int(page_raw)


class ClaimsHandler(BaseHTTPRequestHandler):
    def send_body(self, status: HTTPStatus, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: HTTPStatus, value: object) -> None:
        self.send_body(status, "application/json; charset=utf-8",
                       json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/claims.html"}:
                self.send_body(HTTPStatus.OK, "text/html; charset=utf-8", HTML_PATH.read_bytes())
                return
            if parsed.path in {"/runs", "/runs.html"}:
                self.send_body(HTTPStatus.OK, "text/html; charset=utf-8", RUNS_HTML_PATH.read_bytes())
                return
            if parsed.path in {"/api/group-batches", "/api/group-batch", "/api/group-result"}:
                with ExitStack() as stack:
                    scout = (stack.enter_context(duckdb.connect(str(SCOUT_DB_PATH), read_only=True))
                             if SCOUT_DB_PATH.is_file() else None)
                    if parsed.path == "/api/group-batches":
                        value = {"batches": group_batches(scout)}
                    elif parsed.path == "/api/group-batch":
                        value = group_batch_detail(scout, query.get("batch", [""])[0])
                    else:
                        value = group_result(scout, query.get("batch", [""])[0],
                                             query.get("group", [""])[0])
                self.send_json(HTTPStatus.OK, value)
                return
            if parsed.path not in {"/api/documents", "/api/document", "/api/render", "/api/pdf"}:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
                return
            with ExitStack() as stack:
                text_db = stack.enter_context(duckdb.connect(str(TEXT_DB_PATH), read_only=True))
                if parsed.path in {"/api/documents", "/api/document"}:
                    archive = stack.enter_context(duckdb.connect(str(DB_PATH), read_only=True))
                    scout = (stack.enter_context(duckdb.connect(str(SCOUT_DB_PATH), read_only=True))
                             if SCOUT_DB_PATH.is_file() else None)
                    schema = query_schema(query)
                    if parsed.path == "/api/documents":
                        self.send_json(HTTPStatus.OK, {"schema": schema,
                                                       "documents": document_rows(text_db, archive, scout, schema)})
                    else:
                        self.send_json(HTTPStatus.OK,
                                       document_detail(text_db, archive, scout, query_sha(query),
                                                       schema, query.get("attempt", [None])[0]))
                    return
                if parsed.path == "/api/pdf":
                    sha = query_sha(query)
                    pdf, _ = source_pdf(text_db, sha, 1)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(pdf.stat().st_size))
                    self.send_header("Content-Disposition", f'inline; filename="{sha}.pdf"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with pdf.open("rb") as input_file:
                        while chunk := input_file.read(1024 * 1024):
                            self.wfile.write(chunk)
                    return
                sha, page = query_page(query)
                pdf, _ = source_pdf(text_db, sha, page)
                with tempfile.TemporaryDirectory(prefix="court-claims-") as folder:
                    output = Path(folder) / "page"
                    subprocess.run([
                        "pdftoppm", "-f", str(page), "-l", str(page),
                        "-scale-to", "1800", "-png", "-singlefile",
                        str(pdf), str(output),
                    ], check=True, capture_output=True, timeout=90)
                    png = output.with_suffix(".png").read_bytes()
                self.send_body(HTTPStatus.OK, "image/png", png)
        except (ValueError, LookupError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, subprocess.SubprocessError, duckdb.Error) as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


class ClaimsServer(ThreadingHTTPServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if not TEXT_DB_PATH.is_file() or not DB_PATH.is_file():
        parser.error("The archive and text databases must exist before starting the explorer")
    server = ClaimsServer((args.host, args.port), ClaimsHandler)
    print(f"Claims explorer listening on {display_url(args.host, args.port)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
