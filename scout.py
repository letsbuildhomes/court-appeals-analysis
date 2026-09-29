"""Frozen, resumable scouting batches over the selected court-PDF text."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import duckdb
import httpx

from config import DB_PATH, ROOT, TEXT_DB_PATH


SCOUT_DB_PATH = ROOT / "data" / "scout.duckdb"
LEGACY_PROMPT_PATH = ROOT / "prompts" / "scout_v0.md"
PROMPT_PATH = ROOT / "prompts" / "scout_v3.md"
DEFAULT_LIMIT = 25
MAX_BATCH_SIZE = 100
REASONING_LEVELS = ("none", "low", "high")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def usage_counts(usage: object) -> tuple[int, int | None, int, int] | None:
    """Read standard Chat Completions usage and optional cached-input count."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    output = usage.get("completion_tokens")
    if not isinstance(prompt, int) or isinstance(prompt, bool) or prompt < 0:
        return None
    if not isinstance(output, int) or isinstance(output, bool) or output < 0:
        return None
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens"))
    if cached is not None and (not isinstance(cached, int) or isinstance(cached, bool)
                               or cached < 0 or cached > prompt):
        cached = None
    total = usage.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        total = prompt + output
    return prompt, cached, output, total


def reasoning_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


def parse_reasoning_levels(value: str | None) -> tuple[str | None, ...]:
    if value is None:
        return (None,)
    levels = tuple(part.strip() for part in value.split(","))
    if not levels or any(level not in REASONING_LEVELS for level in levels):
        raise ValueError("--reasoning-levels must contain none, low, and/or high")
    if len(set(levels)) != len(levels):
        raise ValueError("--reasoning-levels cannot repeat a level")
    return levels


def reasoning_from_request(request_json: str) -> str:
    payload = json.loads(request_json)
    return payload.get("reasoning_effort", "default")


def completion_payload(model: str, prompt: str, document: str,
                       response_format: str, reasoning: str | None) -> dict:
    payload = {"model": model, "messages": [
        {"role": "system", "content": prompt},
        {"role": "user", "content": document},
    ], "stream": False}
    if response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    return payload


def init_db(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batches (
            batch_id VARCHAR PRIMARY KEY, created_at VARCHAR NOT NULL,
            seed VARCHAR NOT NULL, ordering VARCHAR NOT NULL,
            prompt_sha256 VARCHAR NOT NULL, document_count INTEGER NOT NULL,
            prompt_path VARCHAR, reasoning_levels VARCHAR
        )
    """)
    batch_columns = {row[0] for row in conn.execute("DESCRIBE batches").fetchall()}
    if "prompt_path" not in batch_columns:
        conn.execute("ALTER TABLE batches ADD COLUMN prompt_path VARCHAR")
    if "reasoning_levels" not in batch_columns:
        conn.execute("ALTER TABLE batches ADD COLUMN reasoning_levels VARCHAR")
    conn.execute("UPDATE batches SET prompt_path = ? WHERE prompt_path IS NULL",
                 [str(LEGACY_PROMPT_PATH)])
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batch_documents (
            batch_id VARCHAR NOT NULL, ordinal INTEGER NOT NULL,
            pdf_sha256 VARCHAR NOT NULL, title VARCHAR,
            listing_date VARCHAR, page_count INTEGER NOT NULL,
            unresolved_pages INTEGER NOT NULL, text_chars BIGINT NOT NULL,
            PRIMARY KEY (batch_id, ordinal)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL,
            pdf_sha256 VARCHAR NOT NULL, attempted_at VARCHAR NOT NULL,
            request_sha256 VARCHAR NOT NULL, input_sha256 VARCHAR NOT NULL,
            prompt_sha256 VARCHAR NOT NULL, model VARCHAR NOT NULL,
            endpoint VARCHAR NOT NULL, response_format VARCHAR NOT NULL,
            status VARCHAR NOT NULL, http_status INTEGER,
            request_json VARCHAR NOT NULL, response_text VARCHAR,
            parsed_json VARCHAR, validation_warnings VARCHAR,
            usage_json VARCHAR, finish_reason VARCHAR,
            elapsed_seconds DOUBLE, error VARCHAR, run_id VARCHAR
        )
    """)
    columns = {row[0] for row in conn.execute("DESCRIBE attempts").fetchall()}
    if "run_id" not in columns:
        conn.execute("ALTER TABLE attempts ADD COLUMN run_id VARCHAR")
    # Earlier fingerprints also hashed the endpoint; rekey them from the stored request.
    conn.execute("""
        UPDATE attempts SET request_sha256 = sha256(request_json)
        WHERE request_sha256 <> sha256(request_json)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL,
            started_at VARCHAR NOT NULL, finished_at VARCHAR,
            model VARCHAR NOT NULL, endpoint VARCHAR NOT NULL,
            requested_count INTEGER NOT NULL, cached_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL
        )
    """)


def run_token_summary(conn: duckdb.DuckDBPyConnection, run_id: str) -> str:
    run = conn.execute("""
        SELECT requested_count, cached_count, failed_count
        FROM runs WHERE run_id = ?
    """, [run_id]).fetchone()
    rows = conn.execute("""
        SELECT usage_json FROM attempts
        WHERE run_id = ? AND status <> 'cached'
    """, [run_id]).fetchall()
    prompt = cached = output = total = known = cache_known = 0
    for (usage_json,) in rows:
        counts = usage_counts(json.loads(usage_json)) if usage_json else None
        if counts is None:
            continue
        known += 1
        prompt += counts[0]
        output += counts[2]
        total += counts[3]
        if counts[1] is not None:
            cached += counts[1]
            cache_known += 1
    cached_label = (f"{cached:,} ({cache_known}/{known} usage responses)"
                    if cache_known else "not reported")
    missing = len(rows) - known
    return (f"Run {run_id}: {run[0]} requests, {run[1]} cached documents, "
            f"{run[2]} failures; tokens: input {prompt:,}, cached input {cached_label}, "
            f"output {output:,}, total {total:,}; "
            f"{known} responses with usage, {missing} without")


def source_metadata() -> dict[str, tuple[str | None, str | None]]:
    """Take the latest listing snapshot; metadata is for sampling, not case facts."""
    result: dict[str, tuple[str | None, str | None]] = {}
    with duckdb.connect(str(DB_PATH), read_only=True) as conn:
        snapshot = conn.execute("SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        if not snapshot:
            raise RuntimeError("No archive listing snapshot found")
        rows = conn.execute("""
            SELECT d.sha256, e.title, e.date_attribute
            FROM listing_entries e JOIN downloads d ON d.media_url = e.media_url
            WHERE e.snapshot_id = ? AND d.status = 'downloaded' AND d.sha256 IS NOT NULL
            ORDER BY e.page_index, e.ordinal
        """, [snapshot[0]]).fetchall()
        for sha, title, date in rows:
            result.setdefault(sha, (title, date))
    return result


def document_inventory() -> list[dict]:
    metadata = source_metadata()
    with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as conn:
        rows = conn.execute("""
            SELECT d.pdf_sha256, count(p.page_number) AS page_count,
                   count(*) FILTER (WHERE p.status = 'unresolved') AS unresolved_pages,
                   sum(length(coalesce(p.text, ''))) AS text_chars
            FROM documents d JOIN current_analysis_pages p USING (pdf_sha256)
            GROUP BY d.pdf_sha256
        """).fetchall()
    docs = []
    for sha, pages, unresolved, chars in rows:
        title, date = metadata.get(sha, (None, None))
        docs.append(dict(sha=sha, title=title, date=date, pages=pages,
                         unresolved=unresolved, chars=chars))
    return docs


def order_documents(docs: list[dict], ordering: str, seed: str) -> list[dict]:
    if ordering == "random":
        return sorted(docs, key=lambda d: digest(seed + ":" + d["sha"]))
    return sorted(docs, key=lambda d: (d["date"] is None, d["date"] or "", d["sha"]))


def render_document(sha: str) -> tuple[str, dict[int, str]]:
    with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as conn:
        pages = conn.execute("""
            SELECT page_number, status, source, text
            FROM current_analysis_pages WHERE pdf_sha256 = ? ORDER BY page_number
        """, [sha]).fetchall()
    if not pages:
        raise RuntimeError(f"No selected pages for {sha}")
    chunks = [f'<document pdf_sha256="{sha}">']
    for number, status, source, selected in pages:
        body = selected if selected else f"[No selected text: {status}]"
        chunks.append(
            f'<page number="{number}" status="{status}" source="{source or "none"}">\n'
            f'{html.escape(body, quote=False)}\n</page>'
        )
    chunks.append("</document>")
    return "\n".join(chunks), {row[0]: row[3] or "" for row in pages}


def plan(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= MAX_BATCH_SIZE:
        raise ValueError(f"--limit must be 1..{MAX_BATCH_SIZE} in scouting mode")
    prompt_file = Path(getattr(args, "prompt", None) or PROMPT_PATH).resolve()
    prompt_hash = digest(prompt_file.read_text(encoding="utf-8"))
    inventory = document_inventory()
    selected_shas = getattr(args, "sha", None)
    if isinstance(selected_shas, str):
        selected_shas = [selected_shas]
    if selected_shas:
        if len(selected_shas) > MAX_BATCH_SIZE:
            raise ValueError(f"A scouting batch can contain at most {MAX_BATCH_SIZE} PDFs")
        by_sha = {doc["sha"]: doc for doc in inventory}
        missing = [sha for sha in selected_shas if sha not in by_sha]
        if missing:
            raise ValueError(f"PDF SHA-256 is not in the selected-text inventory: {missing[0]}")
        if len(set(selected_shas)) != len(selected_shas):
            raise ValueError("--sha cannot repeat a PDF")
        docs = [by_sha[sha] for sha in selected_shas]
    else:
        docs = order_documents(inventory, args.order, args.seed)[:args.limit]
    levels = parse_reasoning_levels(getattr(args, "reasoning_levels", None))
    batch_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    SCOUT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(SCOUT_DB_PATH)) as conn:
        init_db(conn)
        conn.execute("INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     [batch_id, utc_now(), args.seed, args.order, prompt_hash,
                      len(docs), str(prompt_file),
                      ",".join(level for level in levels if level is not None) or None])
        for ordinal, doc in enumerate(docs, 1):
            conn.execute("INSERT INTO batch_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [batch_id, ordinal, doc["sha"], doc["title"], doc["date"],
                          doc["pages"], doc["unresolved"], doc["chars"]])
    print(f"Batch {batch_id}: {len(docs)} documents, {sum(d['chars'] for d in docs):,} "
          f"selected-text characters; prompt {prompt_file.name}; "
          f"reasoning {','.join(level or 'default' for level in levels)}")
    for ordinal, doc in enumerate(docs, 1):
        print(f"{ordinal:2} {doc['sha'][:12]} {doc['pages']:3} pages "
              f"{doc['chars']:7,} chars {doc['unresolved']:2} unresolved "
              f"{(doc['date'] or '')[:10]} {doc['title'] or ''}")


def endpoint_from_env() -> tuple[str, str, str, str]:
    base = os.environ.get("SCOUT_API_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("SCOUT_MODEL", "").strip()
    key = os.environ.get("SCOUT_API_KEY", "")
    response_format = os.environ.get("SCOUT_RESPONSE_FORMAT", "json_object").strip()
    if not base or not model or not key:
        raise ValueError("Set SCOUT_API_BASE_URL, SCOUT_MODEL, and SCOUT_API_KEY in your shell")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("SCOUT_API_BASE_URL must be an HTTP(S) base URL without credentials, query, or fragment")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Use HTTPS for non-local API endpoints")
    if response_format not in {"json_object", "none"}:
        raise ValueError("SCOUT_RESPONSE_FORMAT must be json_object or none")
    if parts.path.endswith("/chat/completions"):
        raise ValueError("SCOUT_API_BASE_URL should end at the API base path, not /chat/completions")
    endpoint = urlunsplit((parts.scheme, parts.netloc,
                           parts.path.rstrip("/") + "/chat/completions", "", ""))
    return endpoint, model, key, response_format


REQUIRED_LISTS = (
    "docket_references", "project_mentions", "participants", "issues",
    "events", "case_relationships", "additional_observations",
)
ENUMS = {
    "document": {
        "kind": {"opinion", "order", "decision", "combined", "other", "unclear"},
        "finality": {"interim", "final_for_issue", "final_for_case", "unclear"},
    },
    "appeal_postures": {
        "track": {"municipal", "act_250", "both", "other", "unclear"},
        "decision_outcome": {"approved", "denied", "approved_with_conditions", "partially_approved", "enforcement_action", "other", "unclear"},
        "appellant_relation": {"applicant_or_developer", "neighbor_or_resident", "municipality", "agency", "other", "unclear"},
        "posture": {"third_party_challenge_to_approval", "applicant_challenge_to_denial", "applicant_challenge_to_conditions", "enforcement_appeal", "other", "unclear"},
    },
    "docket_references": {"role": {"case_decided_here", "related_case", "cited_precedent", "unclear"}},
    "project_mentions": {
        "role": {"subject_project", "related_project", "precedent_or_example", "unclear"},
        "use": {"housing", "mixed_use_with_housing", "commercial", "industrial", "institutional", "infrastructure", "land_use_other", "unclear"},
    },
    "participants": {"role": {"appellant", "appellee", "applicant", "municipality", "neighbor", "agency", "intervenor", "other", "unclear"}},
    "issues": {
        "track": {"municipal", "act_250", "both", "other", "unclear"},
        "topic": {"zoning_standard", "conditional_use", "site_plan", "subdivision", "permit_procedure", "standing", "timeliness", "jurisdiction", "evidence", "remedy", "other", "unclear"},
    },
    "events": {
        "kind": {"municipal_decision", "appeal_filed", "motion_filed", "hearing", "court_ruling", "stay", "remand", "settlement", "higher_court_action", "other", "unclear"},
        "source_role": {"action_in_this_document", "recited_history", "party_claim", "unclear"},
        "finality": {"interim", "final_for_issue", "final_for_case", "unclear"},
    },
    "case_relationships": {"relation": {"consolidated_with", "appeal_of", "remand_to", "same_project_as", "other", "unclear"}},
}

V2_LISTS = (
    "docket_references", "appeals", "participants", "issues", "issue_actions",
    "events", "case_relationships", "schema_suggestions", "uncertainties",
)
V2_ENUMS = {
    "document": {"kind": {"opinion", "order", "decision", "combined", "other", "unclear"}},
    "subject_project": {
        "use": {"housing", "mixed_use_with_housing", "commercial", "industrial",
                "institutional", "infrastructure", "land_use_other", "unclear"},
        "unit_count_basis": {"stated_number", "building_type_conversion", "unclear"},
    },
    "docket_references": {
        "court": {"vt_environmental_division", "vt_supreme_court", "other", "unclear"},
        "role": {"case_decided_here", "related_same_project", "other", "unclear"},
    },
    "appeals": {
        "track": {"municipal", "act_250", "other", "unclear"},
        "posture": {"third_party_challenge_to_approval", "applicant_challenge_to_denial",
                    "applicant_challenge_to_conditions", "enforcement_appeal", "other", "unclear"},
    },
    "appealed_decision": {"outcome": {"approved", "denied", "approved_with_conditions",
                                      "partially_approved", "enforcement_action", "other", "unclear"}},
    "participants": {"entity_type": {"person", "business", "municipality", "agency", "group", "other", "unclear"}},
    "roles": {
        "role": {"developer", "applicant", "decision_maker", "appellant", "appellee",
                 "intervenor", "municipality", "other", "unclear"},
        "scope_kind": {"project", "appealed_decision", "appeal", "docket", "unclear"},
        "basis": {"stated", "inferred", "unclear"},
    },
    "issues": {"topic": {"zoning_standard", "conditional_use", "site_plan", "subdivision",
                         "permit_procedure", "standing", "timeliness", "jurisdiction",
                         "evidence", "remedy", "other", "unclear"}},
    "issue_actions": {
        "action": {"raised", "amended", "narrowed", "withdrawn", "dismissed",
                   "decided", "remanded", "other", "unclear"},
        "disposition_mechanism": {"merits", "summary_judgment", "procedural",
                                  "jurisdictional", "stipulation", "settlement", "other", "unclear"},
    },
    "events": {
        "process_stage": {"municipal_administration", "municipal_panel", "act_250",
                          "environmental_division", "supreme_court", "other", "unclear"},
        "process_effect": {"start", "progress", "end", "unclear"},
        "kind": {"permit_application", "municipal_decision", "appeal_filed", "motion_filed",
                 "hearing", "court_ruling", "stay", "remand", "settlement",
                 "higher_court_action", "other", "unclear"},
        "source_role": {"action_in_this_document", "recited_history", "party_claim", "unclear"},
    },
    "case_relationships": {"relation": {"consolidated_with", "coordinated_with",
                                         "higher_court_review_of", "remand_to",
                                         "same_project_as", "other", "unclear"}},
}


def validate_v2_output(raw: str, pages: dict[int, str]) -> tuple[dict | None, list[str]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON: {exc.msg}"]
    if not isinstance(parsed, dict):
        return None, ["Root must be a JSON object"]
    errors = []
    if parsed.get("schema_version") != "v2":
        errors.append("schema_version must be v2")
    if not isinstance(parsed.get("document"), dict):
        errors.append("document must be an object")
    if parsed.get("subject_project") is not None and not isinstance(parsed["subject_project"], dict):
        errors.append("subject_project must be an object or null")
    for field in V2_LISTS:
        if not isinstance(parsed.get(field), list):
            errors.append(f"{field} must be an array")
    if errors:
        return None, errors
    warnings = []

    def evidence(label: str, item: dict) -> None:
        citations = item.get("evidence")
        if not isinstance(citations, list) or not citations:
            warnings.append(f"{label}: missing evidence")
            return
        for cite in citations:
            if not isinstance(cite, dict) or type(cite.get("page")) is not int or cite["page"] not in pages or not cite.get("quote"):
                warnings.append(f"{label}: invalid page or missing quote")
                continue
            quote = " ".join(html.unescape(str(cite["quote"])).split()).casefold()
            page = " ".join(pages[cite["page"]].split()).casefold()
            if quote not in page:
                warnings.append(f"{label}: quote not found on cited page")

    def check(label: str, group: str, item: object) -> None:
        if not isinstance(item, dict):
            warnings.append(f"{label}: expected object")
            return
        evidence(label, item)
        for field, allowed in V2_ENUMS.get(group, {}).items():
            if item.get(field) not in allowed:
                warnings.append(f"{label}: {field} outside provisional categories")
        for field, value in item.items():
            if field.endswith("_iso") and value is not None and (
                not isinstance(value, str) or
                not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value)
            ):
                warnings.append(f"{label}: {field} must be YYYY, YYYY-MM, YYYY-MM-DD, or null")

    check("document", "document", parsed["document"])
    if parsed["subject_project"] is not None:
        project = parsed["subject_project"]
        check("subject_project", "subject_project", project)
        count = project.get("proposed_housing_units")
        if count is not None and (type(count) is not int or count < 0):
            warnings.append("subject_project: proposed_housing_units must be a nonnegative integer or null")
        building = str(project.get("proposed_building_type_raw") or "").casefold()
        plex_units = {"single-family": 1, "single family": 1, "duplex": 2,
                      "triplex": 3, "quadplex": 4, "fourplex": 4, "sixplex": 6}
        if project.get("unit_count_basis") == "building_type_conversion":
            matches = {units for name, units in plex_units.items() if name in building}
            if len(matches) == 1 and count not in matches:
                warnings.append("subject_project: unit count disagrees with building type conversion")
    issue_ids = set()
    appeal_ids = set()
    for field in V2_LISTS:
        if field == "uncertainties":
            continue
        for index, item in enumerate(parsed[field]):
            label = f"{field}[{index}]"
            check(label, field, item)
            if not isinstance(item, dict):
                continue
            if field == "docket_references" and re.search(r"\s+(?:and|&)\s+\d", str(item.get("docket_raw", "")), re.I):
                warnings.append(f"{label}: combined docket numbers; use one reference per docket")
            if field == "appeals":
                appeal_ids.add(item.get("appeal_id"))
                check(f"{label}.appealed_decision", "appealed_decision", item.get("appealed_decision"))
            if field == "participants":
                for nested, group in (("roles", "roles"), ("associated_people", "associated_people")):
                    values = item.get(nested)
                    if not isinstance(values, list):
                        warnings.append(f"{label}.{nested}: expected array")
                        continue
                    for i, value in enumerate(values):
                        check(f"{label}.{nested}[{i}]", group, value)
            if field == "issues":
                issue_ids.add(item.get("issue_id"))
    for index, item in enumerate(parsed["issue_actions"]):
        if isinstance(item, dict) and item.get("issue_id") not in issue_ids:
            warnings.append(f"issue_actions[{index}]: unknown issue_id")
    for field in ("issues", "events"):
        for index, item in enumerate(parsed[field]):
            if isinstance(item, dict) and item.get("appeal_id") is not None and item["appeal_id"] not in appeal_ids:
                warnings.append(f"{field}[{index}]: unknown appeal_id")
    return parsed, warnings


V3_ENUMS = {
    "document": {"kind": {"opinion", "order", "decision", "combined", "other", "unclear"}},
    "project": {"use": {"housing", "mixed_use_with_housing", "commercial", "industrial",
                        "institutional", "infrastructure", "land_use_other", "unclear"}},
    "entity": {
        "kind": {"business", "municipality", "person", "agency", "group", "other", "unclear"},
        "project_role": {"developer", "municipality", "property_owner", "neighbor",
                         "agency", "other", "unclear"},
    },
    "person": {"role": {"owner", "principal", "attorney", "representative", "other", "unclear"}},
    "process": {"kind": {"municipal_administration", "municipal_panel", "act_250",
                         "environmental_division", "supreme_court", "other", "unclear"}},
    "appeal": {"track": {"municipal", "act_250", "other", "unclear"}},
    "decision": {"outcome": {"approved", "denied", "approved_with_conditions",
                             "partially_approved", "enforcement_action", "other", "unclear"}},
    "event": {
        "kind": {"application", "decision", "appeal", "questions_filed", "motion",
                 "filing", "hearing", "ruling", "stay", "remand", "settlement",
                 "other", "unclear"},
        "effect": {"start", "step", "end", "unclear"},
        "reported_as": {"document_action", "history", "party_claim", "unclear"},
        "outcome": {"approved", "denied", "approved_with_conditions",
                    "partially_approved", "enforcement_action", "other", "unclear"},
    },
    "question": {
        "topic": {"zoning_standard", "conditional_use", "site_plan", "subdivision",
                  "permit_procedure", "standing", "timeliness", "jurisdiction",
                  "evidence", "remedy", "other", "unclear"},
        "action": {"raised", "amended", "narrowed", "withdrawn", "dismissed",
                   "decided", "remanded", "other", "unclear"},
        "mechanism": {"merits", "summary_judgment", "procedural", "jurisdictional",
                      "stipulation", "settlement", "other", "unclear"},
    },
    "link": {"relation": {"consolidated_with", "coordinated_with", "review_of",
                           "remand_to", "same_project_as", "other", "unclear"}},
}


def validate_v3_output(raw: str, pages: dict[int, str]) -> tuple[dict | None, list[str]]:
    """Validate the V3 process hierarchy; flag uncertain evidence without discarding JSON."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON: {exc.msg}"]
    if not isinstance(parsed, dict):
        return None, ["Root must be a JSON object"]
    errors: list[str] = []
    warnings: list[str] = []
    if parsed.get("schema_version") != "v3":
        errors.append("schema_version must be v3")
    if not isinstance(parsed.get("document"), dict):
        errors.append("document must be an object")
    if parsed.get("project") is not None and not isinstance(parsed["project"], dict):
        errors.append("project must be an object or null")
    for field in ("entities", "processes", "uncertainties", "suggestions"):
        if not isinstance(parsed.get(field), list):
            errors.append(f"{field} must be an array")
    if errors:
        return None, errors
    if set(parsed) != {"schema_version", "document", "project", "entities", "processes",
                       "uncertainties", "suggestions"}:
        warnings.append("root: unexpected top-level fields")

    def check(label: str, group: str, item: object, *, cited: bool = True) -> dict | None:
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            return None
        for field, allowed in V3_ENUMS.get(group, {}).items():
            if field not in item and field in {"project_role", "outcome", "mechanism"}:
                continue
            value = item.get(field)
            if not isinstance(value, str) or value not in allowed:
                warnings.append(f"{label}: {field} outside provisional categories")
        for field, value in item.items():
            if field.endswith("_raw") or field.endswith("_id"):
                warnings.append(f"{label}: {field} is not a V3 field")
            if field == "date" and value is not None and (
                not isinstance(value, str) or
                not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value)
            ):
                warnings.append(f"{label}: date must be YYYY, YYYY-MM, YYYY-MM-DD, or omitted")
        if cited:
            citations = item.get("evidence")
            if not isinstance(citations, list) or not citations:
                warnings.append(f"{label}: missing evidence")
            else:
                for cite in citations:
                    if (not isinstance(cite, dict) or type(cite.get("page")) is not int
                            or cite["page"] not in pages or not isinstance(cite.get("quote"), str)
                            or not cite["quote"].strip()):
                        warnings.append(f"{label}: invalid page or missing quote")
                        continue
                    quote = " ".join(html.unescape(cite["quote"]).split()).casefold()
                    page_text = " ".join(pages[cite["page"]].split()).casefold()
                    if quote not in page_text:
                        warnings.append(f"{label}: quote not found on cited page")
        return item

    check("document", "document", parsed["document"])
    if isinstance(parsed["project"], dict):
        project = check("project", "project", parsed["project"])
        units = project.get("proposed_units")
        if units is not None and (type(units) is not int or units < 0):
            warnings.append("project: proposed_units must be a nonnegative integer or omitted")
    for i, item in enumerate(parsed["entities"]):
        label = f"entities[{i}]"
        entity = check(label, "entity", item)
        if entity is None:
            continue
        if not isinstance(entity.get("name"), str) or not entity["name"].strip():
            warnings.append(f"{label}: missing name")
        people = entity.get("people", [])
        if not isinstance(people, list):
            errors.append(f"{label}.people must be an array when present")
            continue
        for j, person in enumerate(people):
            check(f"{label}.people[{j}]", "person", person, cited=False)
    for i, item in enumerate(parsed["processes"]):
        label = f"processes[{i}]"
        process = check(label, "process", item, cited=False)
        if process is None:
            continue
        if not isinstance(process.get("authority"), str) or not process["authority"].strip():
            warnings.append(f"{label}: missing authority")
        dockets = process.get("dockets", [])
        if not isinstance(dockets, list) or any(not isinstance(docket, str) or not docket.strip()
                                                for docket in dockets):
            warnings.append(f"{label}: dockets must be an array of case-number strings")
            dockets = []
        if len(dockets) != len(set(dockets)):
            warnings.append(f"{label}: duplicate docket number")
        for field in ("appeals", "events", "questions", "links"):
            values = process.get(field, [])
            if not isinstance(values, list) or (field == "events" and field not in process):
                errors.append(f"{label}.{field} must be an array{' and is required' if field == 'events' else ' when present'}")
                continue
            for j, value in enumerate(values):
                child_label = f"{label}.{field}[{j}]"
                child_group = {"appeals": "appeal", "events": "event",
                               "questions": "question", "links": "link"}[field]
                child = check(child_label, child_group, value)
                if child is None:
                    continue
                if field == "appeals":
                    check(f"{child_label}.decision", "decision", child.get("decision"), cited=False)
                if field in {"appeals", "questions"} and "docket" in child and child["docket"] not in dockets:
                    warnings.append(f"{child_label}: docket not listed on its process")
                if field == "questions" and not (child.get("label") or child.get("text")):
                    warnings.append(f"{child_label}: needs a label or question text")
    for i, value in enumerate(parsed["uncertainties"]):
        if not isinstance(value, str):
            warnings.append(f"uncertainties[{i}]: expected string")
    for i, value in enumerate(parsed["suggestions"]):
        if not isinstance(value, dict) or not isinstance(value.get("idea"), str) or not isinstance(value.get("why"), str) or type(value.get("page")) is not int or value["page"] not in pages:
            warnings.append(f"suggestions[{i}]: expected idea, why, and valid page")
    return (None, errors) if errors else (parsed, warnings)


def validate_output(raw: str, pages: dict[int, str],
                    require_appeal_postures: bool = False) -> tuple[dict | None, list[str]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"Invalid JSON: {exc.msg}"]
    if not isinstance(parsed, dict):
        return None, ["Root must be a JSON object"]
    errors = []
    if not isinstance(parsed.get("document"), dict):
        errors.append("document must be an object")
    required_lists = REQUIRED_LISTS + (("appeal_postures",) if require_appeal_postures else ())
    for field in required_lists:
        if not isinstance(parsed.get(field), list):
            errors.append(f"{field} must be an array")
    if "appeal_postures" in parsed and not isinstance(parsed["appeal_postures"], list):
        errors.append("appeal_postures must be an array")
    if errors:
        return None, errors
    warnings = []
    if not isinstance(parsed.get("uncertainties"), list):
        warnings.append("uncertainties: expected array")

    def check_evidence(label: str, evidence: object) -> None:
        if not isinstance(evidence, list) or not evidence:
            warnings.append(f"{label}: missing evidence")
            return
        for citation in evidence:
            if not isinstance(citation, dict) or citation.get("page") not in pages or not citation.get("quote"):
                warnings.append(f"{label}: invalid page or missing quote")
                continue
            quote = " ".join(html.unescape(str(citation["quote"])).split()).casefold()
            page_text = " ".join(pages[citation["page"]].split()).casefold()
            if quote not in page_text:
                warnings.append(f"{label}: quote not found on cited page")

    def check_enums(label: str, group: str, item: dict) -> None:
        for field, allowed in ENUMS.get(group, {}).items():
            if item.get(field) not in allowed:
                warnings.append(f"{label}: {field} outside provisional categories")

    claim_lists = REQUIRED_LISTS + (("appeal_postures",) if "appeal_postures" in parsed else ())
    for field in claim_lists:
        for i, claim in enumerate(parsed[field]):
            label = f"{field}[{i}]"
            if not isinstance(claim, dict):
                warnings.append(f"{label}: expected object")
                continue
            check_enums(label, field, claim)
            check_evidence(label, claim.get("evidence"))
    document = parsed["document"]
    check_enums("document", "document", document)
    check_evidence("document", document.get("evidence"))
    return parsed, warnings


def post_completion(client: httpx.Client, endpoint: str, key: str, payload: dict,
                    attempts: int = 3) -> tuple[int, dict | None, str | None]:
    for attempt in range(attempts):
        try:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            return 0, None, "Connection failed before request delivery"
        except httpx.RequestError:
            return 0, None, "Delivery uncertain after network error"
        if response.status_code == 429 and attempt + 1 < attempts:
            retry_after = response.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
            time.sleep(min(delay, 30) + random.uniform(0, 0.3))
            continue
        if response.status_code != 200:
            suffix = "; delivery uncertain" if response.status_code >= 500 else ""
            return response.status_code, None, f"HTTP {response.status_code}{suffix}"
        try:
            return 200, response.json(), None
        except ValueError:
            return 200, None, "Invalid API response JSON"
    raise AssertionError("unreachable")


def request_completion(endpoint: str, key: str, payload: dict,
                       timeout: float) -> tuple[int, dict | None, str | None, float]:
    """Keep network work off the DuckDB writer thread."""
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=30.0)) as client:
        status, response, error = post_completion(client, endpoint, key, payload)
    return status, response, error, time.monotonic() - started


def run(args: argparse.Namespace) -> None:
    endpoint, model, key, response_format = endpoint_from_env()
    workers = getattr(args, "workers", 1)
    with duckdb.connect(str(SCOUT_DB_PATH)) as conn:
        init_db(conn)
        batch = conn.execute("""
            SELECT prompt_sha256, prompt_path, reasoning_levels
            FROM batches WHERE batch_id = ?
        """, [args.batch_id]).fetchone()
        if not batch:
            raise ValueError(f"Unknown batch: {args.batch_id}")
        prompt_file = Path(batch[1])
        prompt = prompt_file.read_text(encoding="utf-8")
        prompt_hash = digest(prompt)
        if batch[0] != prompt_hash:
            raise ValueError("Prompt changed since this batch was planned; make a new batch")
        levels = parse_reasoning_levels(batch[2])
        docs = conn.execute("""
            SELECT ordinal, pdf_sha256 FROM batch_documents
            WHERE batch_id = ? ORDER BY ordinal
        """, [args.batch_id]).fetchall()
        # Check every selected request before sending any. An uncertain prior delivery
        # must never be silently resubmitted during a parallel resume.
        jobs_by_level = []
        for level in levels:
            level_jobs = []
            for ordinal, sha in docs:
                document, pages = render_document(sha)
                payload = completion_payload(model, prompt, document, response_format, level)
                request_json = canonical_json(payload)
                # The endpoint is recorded but not fingerprinted: a proxy or alternate base URL
                # for the same model, prompt, text, and options may reuse a result.
                request_hash = digest(request_json)
                existing = conn.execute("""
                    SELECT 1 FROM attempts WHERE request_sha256 = ? AND status = 'ok' LIMIT 1
                """, [request_hash]).fetchone()
                uncertain = conn.execute("""
                    SELECT 1 FROM attempts WHERE request_sha256 = ?
                    AND status = 'uncertain_delivery' LIMIT 1
                """, [request_hash]).fetchone()
                if uncertain and not existing and not args.retry_uncertain:
                    raise RuntimeError(f"{sha[:12]} {level or 'default'}: prior delivery uncertain; "
                                       "use --retry-uncertain to resend")
                level_jobs.append({
                    "ordinal": ordinal, "sha": sha, "document": document, "pages": pages,
                    "level": level or "default", "payload": payload,
                    "request_json": request_json, "request_hash": request_hash,
                    "cached": bool(existing),
                })
            jobs_by_level.append(level_jobs)
        run_id = uuid.uuid4().hex
        conn.execute("INSERT INTO runs VALUES (?, ?, ?, NULL, ?, ?, 0, 0, 0)",
                     [run_id, args.batch_id, utc_now(), model, endpoint])
        completed = failed = skipped = requests = 0
        next_launch = 0.0
        halt = False
        for level_jobs in jobs_by_level:
            if halt:
                break
            uncached = []
            for job in level_jobs:
                if not job["cached"]:
                    uncached.append(job)
                    continue
                skipped += 1
                marker = conn.execute("""
                    SELECT 1 FROM attempts WHERE batch_id = ? AND request_sha256 = ?
                    AND status IN ('ok', 'cached') LIMIT 1
                """, [args.batch_id, job["request_hash"]]).fetchone()
                if not marker:
                    conn.execute("""
                        INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [uuid.uuid4().hex, args.batch_id, job["sha"], utc_now(),
                           job["request_hash"], digest(job["document"]), prompt_hash,
                           model, endpoint, response_format, "cached", None,
                           job["request_json"], None, None, "[]", None, None, 0.0, None,
                           run_id])
                print(f"[{job['ordinal']}/{len(docs)}] {job['sha'][:12]} "
                      f"{job['level']} cached")
            # Finish an effort round before starting the next. This gives the provider
            # a chance to reuse exact document prefixes across reasoning levels.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {}
                todo = iter(uncached)
                exhausted = False
                while pending or not exhausted:
                    while not halt and len(pending) < workers and not exhausted:
                        job = next(todo, None)
                        if job is None:
                            exhausted = True
                            break
                        gap = next_launch - time.monotonic()
                        if gap > 0:
                            time.sleep(gap)
                        print(f"[{job['ordinal']}/{len(docs)}] {job['sha'][:12]} "
                              f"{job['level']} requesting ({len(job['document']):,} chars)",
                              flush=True)
                        future = pool.submit(request_completion, endpoint, key,
                                             job["payload"], args.timeout)
                        pending[future] = job
                        requests += 1
                        next_launch = time.monotonic() + args.delay
                    if not pending:
                        break
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        job = pending.pop(future)
                        try:
                            http_status, response, error, elapsed = future.result()
                        except Exception as exc:
                            http_status, response, error, elapsed = (
                                0, None, f"Delivery uncertain after worker error: {type(exc).__name__}", 0.0)
                        if response is not None and not isinstance(response, dict):
                            response, error = None, "Invalid API response shape"
                        choice = (response or {}).get("choices", [{}])
                        choice = choice[0] if isinstance(choice, list) and choice else {}
                        message = choice.get("message") if isinstance(choice, dict) else None
                        raw = message.get("content") if isinstance(message, dict) else None
                        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
                        parsed, warnings = None, []
                        if error is None:
                            if not isinstance(raw, str) or not raw.strip():
                                error = "Missing message content"
                            elif finish_reason == "length":
                                error = "Model response reached output limit"
                            elif finish_reason == "content_filter":
                                error = "Model response stopped by content filter"
                            else:
                                if prompt_file.stem == "scout_v3":
                                    parsed, warnings = validate_v3_output(raw, job["pages"])
                                elif prompt_file.stem == "scout_v2":
                                    parsed, warnings = validate_v2_output(raw, job["pages"])
                                else:
                                    parsed, warnings = validate_output(
                                        raw, job["pages"],
                                        require_appeal_postures=prompt_file.stem == "scout_v1")
                                if parsed is None:
                                    error = "; ".join(warnings)
                        if error is None:
                            status = "ok"
                        elif error.startswith("Delivery uncertain") or http_status >= 500:
                            status = "uncertain_delivery"
                        elif http_status == 200:
                            status = "invalid_output"
                        else:
                            status = "api_error"
                        conn.execute("""
                            INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, [uuid.uuid4().hex, args.batch_id, job["sha"], utc_now(),
                               job["request_hash"], digest(job["document"]), prompt_hash,
                               model, endpoint, response_format, status, http_status,
                               job["request_json"], raw,
                               canonical_json(parsed) if parsed is not None else None,
                               canonical_json(warnings),
                               canonical_json(response.get("usage")) if response and response.get("usage") else None,
                               finish_reason, elapsed, error, run_id])
                        if status == "ok":
                            completed += 1
                            print(f"  {job['sha'][:12]} {job['level']} ok; "
                                  f"{len(warnings)} evidence warnings; {elapsed:.1f}s")
                        else:
                            failed += 1
                            print(f"  {job['sha'][:12]} {job['level']} {status}: "
                                  f"{error}; {elapsed:.1f}s", file=sys.stderr)
                            if status == "uncertain_delivery" or http_status in {
                                400, 401, 402, 403, 404, 422, 429,
                            }:
                                halt = True
                    if halt:
                        print("Stopping new requests after request failure; "
                              "saving responses already in flight.", file=sys.stderr)
        conn.execute("""
            UPDATE runs SET finished_at = ?, requested_count = ?, cached_count = ?,
                            failed_count = ? WHERE run_id = ?
        """, [utc_now(), requests, skipped, failed, run_id])
        token_summary = run_token_summary(conn, run_id)
    print(f"Batch {args.batch_id}: {completed} new ok, {skipped} cached, {failed} failed")
    print(token_summary)
    if failed:
        raise SystemExit(1)


def status(args: argparse.Namespace) -> None:
    if not SCOUT_DB_PATH.exists():
        print("No scouting batches yet")
        return
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        batches = conn.execute("""
            SELECT b.batch_id, b.created_at, b.document_count, b.reasoning_levels,
                   count(DISTINCT a.pdf_sha256) FILTER (WHERE a.status IN ('ok', 'cached')) AS ok_docs,
                   count(DISTINCT a.request_sha256) FILTER (WHERE a.status IN ('ok', 'cached')) AS ok_requests,
                   count(*) FILTER (WHERE a.status NOT IN ('ok', 'cached')) AS failed_attempts
            FROM batches b LEFT JOIN attempts a USING (batch_id)
            GROUP BY b.batch_id, b.created_at, b.document_count, b.reasoning_levels
            ORDER BY b.created_at DESC
        """).fetchall()
        has_runs = conn.execute("""
            SELECT count(*) FROM information_schema.tables WHERE table_name = 'runs'
        """).fetchone()[0]
        run_summaries = []
        if has_runs:
            run_ids = conn.execute("""
                SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 20
            """).fetchall()
            run_summaries = [run_token_summary(conn, run_id) for (run_id,) in run_ids]
    for batch_id, created, count, configured_levels, ok_docs, ok_requests, failed in batches:
        expected = count * len(parse_reasoning_levels(configured_levels))
        print(f"{batch_id}  {created}  {ok_docs}/{count} PDFs  "
              f"{ok_requests}/{expected} requests ok  {failed} failed attempts")
    if run_summaries:
        print("Recent runner invocations:")
        for summary in run_summaries:
            print(summary)


def report(args: argparse.Namespace) -> None:
    if not SCOUT_DB_PATH.exists():
        raise ValueError("No scouting database yet")
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        docs = conn.execute("""
            SELECT ordinal, pdf_sha256, title FROM batch_documents
            WHERE batch_id = ? ORDER BY ordinal
        """, [args.batch_id]).fetchall()
        if not docs:
            raise ValueError(f"Unknown or empty batch: {args.batch_id}")
        print(f"Batch {args.batch_id}: {len(docs)} documents")
        levels = conn.execute("SELECT reasoning_levels FROM batches WHERE batch_id = ?",
                              [args.batch_id]).fetchone()[0]
        if levels:
            print("Showing one successful extraction per PDF; use scout-compare for each reasoning level.")
        for ordinal, sha, title in docs:
            attempts = conn.execute("""
                SELECT status, request_sha256, error, validation_warnings
                FROM attempts WHERE batch_id = ? AND pdf_sha256 = ?
                ORDER BY attempted_at DESC, attempt_id DESC
            """, [args.batch_id, sha]).fetchall()
            if not attempts:
                print(f"{ordinal:2} {sha[:12]} pending  {title or ''}")
                continue
            success = next((row for row in attempts if row[0] in {"ok", "cached"}), None)
            if success is None:
                print(f"{ordinal:2} {sha[:12]} {attempts[0][0]}: {attempts[0][2] or ''}")
                continue
            original = conn.execute("""
                SELECT parsed_json, validation_warnings, usage_json
                FROM attempts WHERE request_sha256 = ? AND status = 'ok' LIMIT 1
            """, [success[1]]).fetchone()
            if not original:
                print(f"{ordinal:2} {sha[:12]} cached result missing")
                continue
            parsed = json.loads(original[0])
            warnings = json.loads(original[1] or "[]")
            usage = json.loads(original[2]) if original[2] else {}
            summary = str(parsed.get("document", {}).get("summary") or "").replace("\n", " ")
            if parsed.get("schema_version") == "v3":
                processes = parsed.get("processes", [])
                counts = (f"entities={len(parsed.get('entities', []))} processes={len(processes)} "
                          f"appeals={sum(len(p.get('appeals', [])) for p in processes)} "
                          f"questions={sum(len(p.get('questions', [])) for p in processes)} "
                          f"events={sum(len(p.get('events', [])) for p in processes)}")
            else:
                fields = (("docket_references", "appeals", "issues", "issue_actions", "events")
                          if parsed.get("schema_version") == "v2" else
                          ("docket_references", "project_mentions", "issues", "events", "additional_observations"))
                counts = " ".join(f"{field}={len(parsed.get(field, []))}" for field in fields)
            print(f"{ordinal:2} {sha[:12]} {title or ''}\n"
                  f"   {summary[:220]}\n"
                  f"   {counts} warnings={len(warnings)} tokens={usage.get('total_tokens', '?')}")


def compare(args: argparse.Namespace) -> None:
    """Show paired reasoning usage; quality remains for inspection in Explorer."""
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        batch = conn.execute("SELECT reasoning_levels FROM batches WHERE batch_id = ?",
                             [args.batch_id]).fetchone()
        if batch is None:
            raise ValueError(f"Unknown batch: {args.batch_id}")
        levels = parse_reasoning_levels(batch[0])
        docs = conn.execute("""
            SELECT ordinal, pdf_sha256, title FROM batch_documents
            WHERE batch_id = ? ORDER BY ordinal
        """, [args.batch_id]).fetchall()
        rows = conn.execute("""
            SELECT pdf_sha256, request_sha256, request_json, status, usage_json,
                   validation_warnings, error
            FROM attempts WHERE batch_id = ?
            ORDER BY attempted_at DESC, attempt_id DESC
        """, [args.batch_id]).fetchall()
        by_document = {}
        for row in rows:
            level = reasoning_from_request(row[2])
            by_document.setdefault((row[0], level), []).append(row)
        sums = {level or "default": {"jobs": 0, "input": 0, "cached": 0,
                                      "output": 0, "reasoning": 0, "total": 0,
                                      "missing_usage": 0, "missing_reasoning": 0}
                for level in levels}
        print(f"Batch {args.batch_id}: {len(docs)} PDFs × {len(levels)} reasoning levels")
        for ordinal, sha, title in docs:
            print(f"{ordinal:2} {sha[:12]} {title or ''}")
            for level in levels:
                name = level or "default"
                attempts = by_document.get((sha, name), [])
                if not attempts:
                    print(f"   {name:7} pending")
                    continue
                row = next((item for item in attempts if item[3] in {"ok", "cached"}), attempts[0])
                status = row[3]
                if status == "cached":
                    original = conn.execute("""
                        SELECT usage_json, validation_warnings FROM attempts
                        WHERE request_sha256 = ? AND status = 'ok' LIMIT 1
                    """, [row[1]]).fetchone()
                    usage_json, warnings_json = original if original else (None, None)
                else:
                    usage_json, warnings_json = row[4], row[5]
                usage = json.loads(usage_json) if usage_json else None
                counts = usage_counts(usage)
                reasoning = reasoning_tokens(usage)
                warnings = json.loads(warnings_json or "[]")
                stats = sums[name]
                if counts:
                    stats["jobs"] += 1
                    stats["input"] += counts[0]
                    stats["cached"] += counts[1] or 0
                    stats["output"] += counts[2]
                    stats["total"] += counts[3]
                    if reasoning is None:
                        stats["missing_reasoning"] += 1
                    else:
                        stats["reasoning"] += reasoning
                    visible = counts[2] - reasoning if reasoning is not None else "?"
                    token_line = (f"input={counts[0]:,} cache_hit={counts[1] if counts[1] is not None else '?'} "
                                  f"output={counts[2]:,} reasoning={reasoning if reasoning is not None else '?'} "
                                  f"visible={visible} total={counts[3]:,}")
                else:
                    stats["missing_usage"] += 1
                    token_line = "usage unavailable"
                suffix = f"warnings={len(warnings)}" if status in {"ok", "cached"} else row[6] or ""
                print(f"   {name:7} {status:17} {token_line} {suffix}")
        print("Token sums by level (including original usage for locally reused results):")
        for name, stats in sums.items():
            print(f"   {name:7} {stats['jobs']} usage responses; input={stats['input']:,} "
                  f"cache_hit={stats['cached']:,} output={stats['output']:,} "
                  f"reasoning={stats['reasoning']:,} total={stats['total']:,}; "
                  f"missing usage={stats['missing_usage']}, "
                  f"missing reasoning breakdown={stats['missing_reasoning']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="freeze a no-cost scouting sample")
    planning.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    planning.add_argument("--order", choices=["random", "chronological"], default="random")
    planning.add_argument("--seed", default="scout-v3")
    planning.add_argument("--sha", action="append", default=None,
                          help="select this full PDF SHA-256; repeat for a fixed cohort")
    planning.add_argument("--reasoning-levels", default=None,
                          help="comma-separated none,low,high; omitted keeps provider default")
    planning.add_argument("--prompt", default=None,
                          help="versioned prompt file; defaults to prompts/scout_v3.md")
    running = commands.add_parser("run", help="run or resume a frozen batch")
    running.add_argument("--batch-id", required=True)
    running.add_argument("--timeout", type=float, default=300)
    running.add_argument("--delay", type=float, default=0.5)
    running.add_argument("--workers", type=int, default=1,
                         help="maximum simultaneous API requests (default 1)")
    running.add_argument("--retry-uncertain", action="store_true",
                         help="resend requests whose prior delivery may have succeeded")
    commands.add_parser("status", help="show checkpointed batches")
    reporting = commands.add_parser("report", help="summarize a completed or partial batch")
    reporting.add_argument("--batch-id", required=True)
    comparison = commands.add_parser("compare", help="compare token usage by reasoning level")
    comparison.add_argument("--batch-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "run" and (args.timeout <= 0 or args.delay < 0 or args.workers < 1 or args.workers > 8):
            raise ValueError("timeout must be positive, delay nonnegative, and workers 1..8")
        {"plan": plan, "run": run, "status": status, "report": report,
         "compare": compare}[args.command](args)
    except (ValueError, RuntimeError, OSError, duckdb.Error) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
