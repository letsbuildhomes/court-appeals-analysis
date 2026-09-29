"""Two-turn scouting of docket groups: triage, then extraction for in-scope groups.

Each group from `groups.py` is sent as one conversation. Turn 1 checks document
membership and study scope with a short JSON answer. Code routes the group:
out-of-scope groups stop, and the rest receive turn 2, which extracts the case
history in the same conversation so the provider can reuse the cached prefix.
Every turn is saved in `data/scout.duckdb` with its request, response, parsed
JSON, validation warnings, and token usage.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from config import ROOT, TEXT_DB_PATH
from groups import GROUPS_DB_PATH, normalize_dockets
from json_repair import repair_json
from scout import (REASONING_LEVELS, SCOUT_DB_PATH, canonical_json, digest,
                   endpoint_from_env, reasoning_tokens, request_completion,
                   usage_counts, utc_now)


PROMPT_SETS = {
    "v4": {"system": ROOT / "prompts" / "group_v4_system.md",
           "triage": ROOT / "prompts" / "group_v4_triage.md",
           "extract": ROOT / "prompts" / "group_v4_extract.md"},
    "v4.1": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v41_triage.md",
             "extract": ROOT / "prompts" / "group_v41_extract.md"},
    "v4.2": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v42_triage.md",
             "extract": ROOT / "prompts" / "group_v42_extract.md"},
    "v4.3": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v43_triage.md",
             "extract": ROOT / "prompts" / "group_v43_extract.md"},
    "v4.4": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v44_triage.md",
             "extract": ROOT / "prompts" / "group_v44_extract.md"},
    "v4.5": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v44_triage.md",
             "extract": ROOT / "prompts" / "group_v45_extract.md"},
    "v4.6": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v44_triage.md",
             "extract": ROOT / "prompts" / "group_v46_extract.md"},
    "v4.7": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v47_triage.md",
             "extract": ROOT / "prompts" / "group_v47_extract.md"},
    "v4.8": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v47_triage.md",
             "extract": ROOT / "prompts" / "group_v48_extract.md"},
    "v4.9": {"system": ROOT / "prompts" / "group_v4_system.md",
             "triage": ROOT / "prompts" / "group_v47_triage.md",
             "extract": ROOT / "prompts" / "group_v49_extract.md"},
    "v4.10": {"system": ROOT / "prompts" / "group_v4_system.md",
              "triage": ROOT / "prompts" / "group_v410_triage.md",
              "extract": ROOT / "prompts" / "group_v410_extract.md"},
    "v5": {"system": ROOT / "prompts" / "group_v5_system.md",
           "triage": ROOT / "prompts" / "group_v5_triage.md",
           "extract": ROOT / "prompts" / "group_v5_extract.md"},
    "v6": {"system": ROOT / "prompts" / "group_v5_system.md",
           "triage": ROOT / "prompts" / "group_v6_triage.md",
           "extract": ROOT / "prompts" / "group_v6_extract.md"},
}
DEFAULT_PROMPT_SET = "v4.3"
FLAT_VERSIONS = {"v4.1", "v4.2", "v4.3", "v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10"}
V5_TRIAGE = "v5-triage"
# Keys that only occur in the top-level object of each output schema; used by
# the deterministic JSON repair to close containers the model left open.
ROOT_KEYS = {
    "v4-triage": frozenset({"schema_version", "documents", "matter", "municipal_appeal",
                            "housing", "reason"}),
    "v4.1-triage": frozenset({"schema_version", "documents", "matter", "case_type", "development",
                              "municipal_appeal", "housing", "reason"}),
    "v4": frozenset({"schema_version", "project", "processes", "uncertainties", "suggestions"}),
    "v4.1": frozenset({"schema_version", "project", "processes", "participants", "grounds",
                       "remarks", "events", "uncertainties", "suggestions"}),
}
for _version in ("v4.2", "v4.3", "v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10"):
    ROOT_KEYS[f"{_version}-triage"] = ROOT_KEYS["v4.1-triage"]
    ROOT_KEYS[_version] = ROOT_KEYS["v4.1"]
ROOT_KEYS[V5_TRIAGE] = frozenset({"schema_version", "documents", "matter", "municipal_appeal",
                                   "housing", "reason", "evidence"})
ROOT_KEYS["v5"] = frozenset({"schema_version", "dockets", "scouting"})
ROOT_KEYS["v6"] = ROOT_KEYS["v5"]
# USD per million tokens (DeepSeek Flash, September 2026).
PRICES_PER_M = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6}
MAX_BATCH_GROUPS = 50
DEFAULT_MAX_CHARS = 400_000
HALT_STATUSES = {400, 401, 402, 403, 404, 422, 429}

TRIAGE_ENUMS = {"belongs": {"yes", "no", "unclear"},
                "scope": {"yes", "no", "unclear"},
                "case_type": {"municipal_permit_appeal", "municipal_enforcement", "act_250",
                              "state_permit", "state_enforcement", "bylaw_or_policy", "other",
                              "unclear"},
                "development": {"new_housing", "existing_home", "commercial", "industrial",
                                "institutional", "infrastructure", "agricultural",
                                "mixed_with_housing", "other", "unclear"}}
V4_ENUMS = {
    "process": {"level": {"municipal", "act_250", "state_agency", "environmental_division",
                          "supreme_court", "other"}},
    "event": {
        "kind": {"application", "decision", "appeal", "stay", "remand", "ruling",
                 "settlement", "withdrawal", "other"},
        "outcome": {"approved", "approved_with_conditions", "denied", "affirmed", "reversed",
                    "remanded", "dismissed", "mixed", "interim", "granted", "other"},
        "mechanism": {"merits", "summary_judgment", "standing", "jurisdiction", "timeliness",
                      "procedural", "stipulation", "withdrawal", "other"},
    },
    "effect": {"measure": {"units", "lots", "floor_area", "land", "other"},
               "change": {"removed", "reduced", "increased", "conditioned", "restored", "other"}},
    "participant": {
        "kind": {"person", "business", "group", "municipality", "agency", "other"},
        "relation": {"developer", "neighbor", "resident_group", "advocacy_group",
                     "municipality", "agency", "other", "unclear"},
        "counsel": {"represented", "pro_se"},
    },
    "ground": {
        "topic": {"use", "density", "dimensional", "design", "traffic", "character",
                  "environment", "infrastructure", "procedure", "standing", "jurisdiction",
                  "other"},
        "category": {"objective", "discretionary", "procedural", "mixed", "unclear"},
        "result": {"succeeded", "failed", "dismissed", "withdrawn", "undecided", "mixed"},
    },
    "remark": {"kind": {"criticism", "sanction", "fees", "warning", "other"}},
}
PARTICIPANT_ROLES = {"appellant", "cross_appellant", "applicant", "interested_person", "other"}
REQUIRED_ENUMS = {"process": {"level"}, "event": {"kind"}, "effect": {"measure", "change"},
                  "participant": {"kind", "relation"},
                  "ground": {"topic", "category", "result"}, "remark": {"kind"}}


def now_precise() -> str:
    """Microsecond UTC time so the latest turn of a group is unambiguous."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Database


def init_db(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_batches (
            batch_id VARCHAR PRIMARY KEY, created_at VARCHAR NOT NULL, label VARCHAR,
            grouping_version VARCHAR NOT NULL, prompt_sha256 VARCHAR NOT NULL,
            system_prompt VARCHAR NOT NULL, triage_prompt VARCHAR NOT NULL,
            extract_prompt VARCHAR NOT NULL, triage_reasoning VARCHAR,
            extract_reasoning VARCHAR, max_chars BIGINT NOT NULL, group_count INTEGER NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_batch_members (
            batch_id VARCHAR NOT NULL, ordinal INTEGER NOT NULL, group_id VARCHAR NOT NULL,
            dockets VARCHAR[] NOT NULL, composition VARCHAR NOT NULL,
            documents_json VARCHAR NOT NULL, text_chars BIGINT NOT NULL, note VARCHAR,
            PRIMARY KEY (batch_id, ordinal)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_runs (
            run_id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL, started_at VARCHAR NOT NULL,
            finished_at VARCHAR, model VARCHAR NOT NULL, endpoint VARCHAR NOT NULL,
            requested_count INTEGER NOT NULL, cached_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_turns (
            turn_id VARCHAR PRIMARY KEY, batch_id VARCHAR NOT NULL, group_id VARCHAR NOT NULL,
            step VARCHAR NOT NULL, run_id VARCHAR, attempted_at VARCHAR NOT NULL,
            request_sha256 VARCHAR NOT NULL, input_sha256 VARCHAR NOT NULL,
            model VARCHAR NOT NULL, endpoint VARCHAR NOT NULL, reasoning VARCHAR,
            status VARCHAR NOT NULL, http_status INTEGER, request_json VARCHAR NOT NULL,
            response_text VARCHAR, parsed_json VARCHAR, validation_warnings VARCHAR,
            usage_json VARCHAR, finish_reason VARCHAR, elapsed_seconds DOUBLE,
            error VARCHAR, route VARCHAR, route_json VARCHAR, reused_turn_id VARCHAR
        )""")


# ---------------------------------------------------------------------------
# Input


def load_group(conn: duckdb.DuckDBPyConnection, group_id: str) -> dict:
    row = conn.execute("""
        SELECT group_id, dockets, composition, text_chars FROM groups WHERE group_id = ?
    """, [group_id]).fetchone()
    if row is None:
        raise ValueError(f"Unknown group: {group_id}")
    docs = conn.execute("""
        SELECT ordinal, pdf_sha256, listing_title, listing_date, listing_dockets,
               caption_dockets, basis, flags, pages, text_chars
        FROM group_documents WHERE group_id = ? ORDER BY ordinal
    """, [group_id]).fetchall()
    return {"group_id": row[0], "dockets": list(row[1]), "composition": row[2],
            "text_chars": row[3], "documents": [
                {"doc": d[0], "sha": d[1], "title": d[2], "posted": d[3],
                 "listing_dockets": list(d[4]), "caption_dockets": list(d[5]),
                 "basis": d[6], "flags": list(d[7]), "pages": d[8], "chars": d[9]}
                for d in docs]}


def render_group(group_id: str, documents: list[dict]) -> tuple[str, dict[int, dict[int, str]]]:
    """Page-tagged group text and each document's page text for evidence checks."""
    shas = [d["sha"] for d in documents]
    with duckdb.connect(str(TEXT_DB_PATH), read_only=True) as conn:
        rows = conn.execute("""
            SELECT pdf_sha256, page_number, status, source, text
            FROM current_analysis_pages WHERE list_contains(?, pdf_sha256)
            ORDER BY pdf_sha256, page_number
        """, [shas]).fetchall()
    pages_by_sha: dict[str, list[tuple]] = {}
    for sha, number, status, source, text in rows:
        pages_by_sha.setdefault(sha, []).append((number, status, source, text))
    chunks = [f'<group documents="{len(documents)}">']
    pages: dict[int, dict[int, str]] = {}
    for d in documents:
        if d["sha"] not in pages_by_sha:
            raise RuntimeError(f"No selected pages for {d['sha']}")
        attrs = " ".join(f'{name}="{html.escape(value)}"' for name, value in (
            ("doc", str(d["doc"])), ("basis", d["basis"]),
            ("caption_dockets", "; ".join(d["caption_dockets"])),
            ("listing_dockets", "; ".join(d["listing_dockets"])),
            ("flags", "; ".join(d["flags"]))))
        chunks.append(f"<document {attrs}>")
        pages[d["doc"]] = {}
        for number, status, source, text in pages_by_sha[d["sha"]]:
            body = text if text else f"[No selected text: {status}]"
            chunks.append(f'<page number="{number}" status="{status}" source="{source or "none"}">\n'
                          f'{html.escape(body, quote=False)}\n</page>')
            pages[d["doc"]][number] = text or ""
        chunks.append("</document>")
    chunks.append("</group>")
    return "\n".join(chunks), pages


def triage_payload(model: str, prompts: dict[str, str], group_text: str,
                   reasoning: str | None) -> dict:
    payload = {"model": model, "stream": False, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": prompts["system"]},
                            {"role": "user", "content": group_text + "\n\n" + prompts["triage"]}]}
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    return payload


def extract_payload(triage: dict, triage_response: str, prompts: dict[str, str],
                    reasoning: str | None) -> dict:
    """Continue the triage conversation; its messages form the cacheable prefix."""
    payload = {key: value for key, value in triage.items() if key != "reasoning_effort"}
    payload["messages"] = triage["messages"] + [
        {"role": "assistant", "content": triage_response},
        {"role": "user", "content": prompts["extract"]}]
    if reasoning is not None:
        payload["reasoning_effort"] = reasoning
    return payload


# ---------------------------------------------------------------------------
# Validation


PUNCTUATION = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                             "\u2032": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
                             "\u2033": '"', "\u2013": "-", "\u2014": "-", "\u2212": "-",
                             "\u00a0": " ", "\u00ad": None})


def comparable(text: str) -> str:
    """Casefolded text with typographic quotes, dashes, and spacing made plain."""
    plain = " ".join(html.unescape(text).translate(PUNCTUATION).split())
    return re.sub(r"(\w)- (\w)", r"\1-\2", plain).casefold()  # hyphen split at a line break


ELLIPSIS_RE = re.compile(r"\s*(?:\.\s?\.\s?\.|\u2026)\s*")


def quote_on_page(quote: str, page: str, following: str = "") -> bool:
    """Whether a quote is on a page (or runs onto the next one), tolerating
    elisions, footnote markers, and old encodings of quotation marks."""
    text = comparable(page + " " + following)
    q = comparable(quote)
    if q in text:
        return True
    pieces = [piece for piece in ELLIPSIS_RE.split(q) if len(piece) >= 8]
    if len(pieces) > 1:
        position = 0
        for piece in pieces:
            found = text.find(piece, position)
            if found < 0:
                break
            position = found + len(piece)
        else:
            return True
    # Near-exact: around the longest shared run, 90% of the quote's characters
    # align with one passage no more than 1.4 times the quote's length.
    if len(q) >= 20:
        anchor = difflib.SequenceMatcher(None, q, text, autojunk=False).find_longest_match(
            0, len(q), 0, len(text))
        if anchor.size >= 12:
            start = max(0, anchor.b - anchor.a - 10)
            window = text[start:start + int(len(q) * 1.4) + 20]
            matcher = difflib.SequenceMatcher(None, q, window, autojunk=False)
            if sum(block.size for block in matcher.get_matching_blocks()) >= 0.9 * len(q):
                return True
    return False


def locate_quote(quote: str, pages: dict[int, dict[int, str]], doc: int,
                 page: int) -> tuple[int, int] | None:
    """The cited (doc, page) if the quote is there, else the best other location.

    Preference: the same document before others, and a quote wholly on one page
    before one that runs onto the next; ties go to the earliest page.
    """
    order = [(doc, page)] + [(doc, p) for p in sorted(pages[doc]) if p != page] + [
        (d, p) for d in sorted(pages) if d != doc for p in sorted(pages[d])]
    for spanning in (False, True):
        for d, p in order:
            following = pages[d].get(p + 1, "") if spanning else ""
            if quote_on_page(quote, pages[d][p], following):
                return d, p
    return None


def check_evidence(label: str, evidence: object, pages: dict[int, dict[int, str]],
                   warnings: list[str], excluded: set[int] = frozenset()) -> None:
    """Check citations; move a citation whose quote is uniquely found elsewhere."""
    if not isinstance(evidence, list) or not evidence:
        warnings.append(f"{label}: missing evidence")
        return
    for i, cite in enumerate(evidence):
        where = f"{label}.evidence[{i}]"
        if isinstance(cite, str) and cite.strip():
            # A bare quote string: recover its document and page by searching the group.
            first_doc = min(pages)
            found = locate_quote(cite, pages, first_doc, min(pages[first_doc]))
            if found is None:
                warnings.append(f"{where}: bare quote string not found in the group")
                continue
            evidence[i] = cite = {"doc": found[0], "page": found[1], "quote": cite}
            warnings.append(f"{where}: bare quote located at doc {found[0]} page {found[1]}")
        if not isinstance(cite, dict) or type(cite.get("doc")) is not int \
                or type(cite.get("page")) is not int or not isinstance(cite.get("quote"), str) \
                or not cite["quote"].strip():
            warnings.append(f"{where}: needs integer doc and page and a quote")
            continue
        if cite["doc"] not in pages or cite["page"] not in pages[cite["doc"]]:
            warnings.append(f"{where}: doc {cite['doc']} page {cite['page']} does not exist")
            continue
        found = locate_quote(cite["quote"], pages, cite["doc"], cite["page"])
        if found is None:
            warnings.append(f"{where}: quote not found on doc {cite['doc']} page {cite['page']}")
        elif found != (cite["doc"], cite["page"]):
            warnings.append(f"{where}: citation corrected from doc {cite['doc']} page "
                            f"{cite['page']} to doc {found[0]} page {found[1]}")
            cite["cited_as"] = {"doc": cite["doc"], "page": cite["page"]}
            cite["doc"], cite["page"] = found
        if cite["doc"] in excluded:
            warnings.append(f"{where}: cites doc {cite['doc']}, which triage excluded")


def check_enums(label: str, group: str, item: dict, warnings: list[str]) -> None:
    for field in REQUIRED_ENUMS.get(group, set()) - set(item):
        warnings.append(f"{label}: missing {field}")
    for field, allowed in V4_ENUMS.get(group, {}).items():
        if field in item and item[field] not in allowed:
            warnings.append(f"{label}: {field} {item[field]!r} is not an allowed value")


def check_date(label: str, value: object, warnings: list[str]) -> None:
    if value is None:
        return
    valid = isinstance(value, str) and len(value) in {4, 7, 10}
    if valid:
        try:
            datetime.strptime(value, {4: "%Y", 7: "%Y-%m", 10: "%Y-%m-%d"}[len(value)])
        except ValueError:
            valid = False
    if not valid:
        warnings.append(f"{label}: date must be YYYY, YYYY-MM, or YYYY-MM-DD")


def parse_object(raw: str) -> tuple[dict | None, str | None]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "Root must be a JSON object"
    return parsed, None


def schema_of(prompt: str) -> str:
    """The schema version a step prompt asks for, from its JSON skeleton."""
    match = re.search(r'"schema_version":"([^"]+)"', prompt)
    if not match:
        raise ValueError("Prompt has no schema_version skeleton")
    return match[1]


def parse_output(raw: str, version: str) -> tuple[str, list[str]]:
    """Repair bracket slips and a misspelled schema_version deterministically;
    return JSON text and notes describing each change."""
    # Repair hints must name keys unique to the root. In V5, `dockets` also
    # appears inside timeline entries and must never pull those entries outward.
    repair_keys = (frozenset({"schema_version", "scouting"}) if version in {"v5", "v6"}
                   else ROOT_KEYS.get(version, frozenset()))
    value, notes = repair_json(raw, repair_keys)
    fixes = [f"json repaired: {'; '.join(notes)}"] if value is not None and notes else []
    if isinstance(value, dict) and value.get("schema_version") != version and \
            str(value.get("schema_version", "")).strip().lower().lstrip("v") == version.lstrip("v"):
        fixes.append(f"schema_version {value['schema_version']!r} normalized to {version!r}")
        value["schema_version"] = version
    if not fixes:
        return raw, []
    return json.dumps(value, ensure_ascii=False), fixes


def validate_triage(raw: str, pages: dict[int, dict[int, str]],
                    version: str = "v4-triage") -> tuple[dict | None, list[str]]:
    raw, repairs = parse_output(raw, version)
    parsed, error = parse_object(raw)
    if error:
        return None, [error]
    errors, warnings = [], list(repairs)
    if parsed.get("schema_version") != version:
        errors.append(f"schema_version must be {version}")
    if version in {f"{v}-triage" for v in FLAT_VERSIONS}:
        for field in ("case_type", "development"):
            if parsed.get(field) not in TRIAGE_ENUMS[field]:
                warnings.append(f"triage: {field} {parsed.get(field)!r} is not an allowed value")
    for field in ("municipal_appeal", "housing"):
        if parsed.get(field) not in TRIAGE_ENUMS["scope"]:
            errors.append(f"{field} must be yes, no, or unclear")
    if not isinstance(parsed.get("documents"), list):
        errors.append("documents must be an array")
    if errors:
        return None, errors
    seen = set()
    for i, item in enumerate(parsed["documents"]):
        label = f"documents[{i}]"
        if not isinstance(item, dict) or item.get("doc") not in pages:
            warnings.append(f"{label}: unknown doc")
            continue
        if item["doc"] in seen:
            warnings.append(f"{label}: doc {item['doc']} repeated")
        seen.add(item["doc"])
        if item.get("belongs") not in TRIAGE_ENUMS["belongs"]:
            warnings.append(f"{label}: belongs must be yes, no, or unclear")
        if not isinstance(item.get("caption_dockets", []), list):
            warnings.append(f"{label}: caption_dockets must be an array")
        check_date(label, item.get("date"), warnings)
    for doc in sorted(set(pages) - seen):
        warnings.append(f"documents: doc {doc} missing; treated as unclear")
    check_evidence("triage", parsed.get("evidence"), pages, warnings)
    return parsed, warnings


def route_triage(triage: dict, docs: list[int]) -> dict:
    """Deterministic routing: stop only on an explicit no."""
    belongs = {item.get("doc"): item.get("belongs") for item in triage.get("documents", [])
               if isinstance(item, dict)}
    excluded = [doc for doc in docs if belongs.get(doc) == "no"]
    if len(excluded) == len(docs):
        route = "no_members"
    elif triage.get("municipal_appeal") == "no":
        route = "out_of_scope"
    elif triage.get("housing") == "no":
        route = "out_of_scope"
    else:
        route = "extract"
    detail = {"route": route, "excluded_docs": excluded,
              "municipal_appeal": triage.get("municipal_appeal"), "housing": triage.get("housing")}
    for field in ("case_type", "development"):
        if field in triage:
            detail[field] = triage[field]
    return detail


def validate_extract(raw: str, pages: dict[int, dict[int, str]],
                     excluded: set[int] = frozenset(),
                     version: str = "v4") -> tuple[dict | None, list[str]]:
    raw, repairs = parse_output(raw, version)
    if version in {"v5", "v6"}:
        parsed, warnings = validate_focused_extract(raw, pages, excluded, version)
        return parsed, (repairs + warnings) if parsed is not None else warnings
    if version in FLAT_VERSIONS:
        parsed, warnings = validate_flat_extract(raw, pages, excluded, version)
        return parsed, (repairs + warnings) if parsed is not None else warnings
    parsed, error = parse_object(raw)
    if error:
        return None, [error]
    errors, warnings = [], list(repairs)
    if parsed.get("schema_version") != "v4":
        errors.append("schema_version must be v4")
    if parsed.get("project") is not None and not isinstance(parsed.get("project"), dict):
        errors.append("project must be an object or null")
    for field in ("processes", "uncertainties", "suggestions"):
        if not isinstance(parsed.get(field), list):
            errors.append(f"{field} must be an array")
    if errors:
        return None, errors

    def items(label: str, value: object) -> list[tuple[str, dict]]:
        if value is None:
            return []
        if not isinstance(value, list):
            warnings.append(f"{label}: must be an array")
            return []
        out = []
        for i, item in enumerate(value):
            if isinstance(item, dict):
                out.append((f"{label}[{i}]", item))
            else:
                warnings.append(f"{label}[{i}]: expected object")
        return out

    project = parsed["project"]
    if project:
        for field in ("units", "lots"):
            if field in project and (type(project[field]) is not int or project[field] < 0):
                warnings.append(f"project: {field} must be a nonnegative integer")
        check_evidence("project", project.get("evidence"), pages, warnings, excluded)
    for label, process in items("processes", parsed["processes"]):
        check_enums(label, "process", process, warnings)
        if not isinstance(process.get("body"), str) or not process["body"].strip():
            warnings.append(f"{label}: missing body")
        if "events" not in process:
            warnings.append(f"{label}: events is required")
        for ev_label, event in items(f"{label}.events", process.get("events")):
            check_enums(ev_label, "event", event, warnings)
            check_date(ev_label, event.get("date"), warnings)
            if "final" in event and not isinstance(event["final"], bool):
                warnings.append(f"{ev_label}: final must be true or false")
            for eff_label, effect in items(f"{ev_label}.effects", event.get("effects")):
                check_enums(eff_label, "effect", effect, warnings)
            check_evidence(ev_label, event.get("evidence"), pages, warnings, excluded)
        for p_label, person in items(f"{label}.participants", process.get("participants")):
            check_enums(p_label, "participant", person, warnings)
            roles = person.get("roles")
            if not isinstance(roles, list) or not roles or not set(roles) <= PARTICIPANT_ROLES:
                warnings.append(f"{p_label}: roles must be a non-empty list of allowed roles")
            check_evidence(p_label, person.get("evidence"), pages, warnings, excluded)
        for g_label, ground in items(f"{label}.grounds", process.get("grounds")):
            check_enums(g_label, "ground", ground, warnings)
            if "applies" in ground and not isinstance(ground["applies"], bool):
                warnings.append(f"{g_label}: applies must be true or false")
            check_evidence(g_label, ground.get("evidence"), pages, warnings, excluded)
        for r_label, remark in items(f"{label}.remarks", process.get("remarks")):
            check_enums(r_label, "remark", remark, warnings)
            check_evidence(r_label, remark.get("evidence"), pages, warnings, excluded)
    for i, value in enumerate(parsed["uncertainties"]):
        if not isinstance(value, str):
            warnings.append(f"uncertainties[{i}]: expected string")
    for i, value in enumerate(parsed["suggestions"]):
        if not isinstance(value, dict) or not isinstance(value.get("idea"), str):
            warnings.append(f"suggestions[{i}]: expected idea and why")
    return parsed, warnings


def validate_flat_extract(raw: str, pages: dict[int, dict[int, str]],
                          excluded: set[int] = frozenset(),
                          version: str = "v4.1") -> tuple[dict | None, list[str]]:
    """V4.1+: processes plus top-level lists that point at a process by position."""
    parsed, error = parse_object(raw)
    if error:
        return None, [error]
    errors, warnings = [], []
    if parsed.get("schema_version") != version:
        errors.append(f"schema_version must be {version}")
    if parsed.get("project") is not None and not isinstance(parsed.get("project"), dict):
        errors.append("project must be an object or null")
    for field in ("processes", "participants", "grounds", "remarks", "events",
                  "uncertainties", "suggestions"):
        if not isinstance(parsed.get(field, []), list):
            errors.append(f"{field} must be an array")
    if not isinstance(parsed.get("processes"), list):
        errors.append("processes is required")
    if errors:
        return None, errors
    processes = parsed["processes"]
    project = parsed["project"]
    if project:
        for field in ("units", "lots"):
            if field in project and (type(project[field]) is not int or project[field] < 0):
                warnings.append(f"project: {field} must be a nonnegative integer")
        check_evidence("project", project.get("evidence"), pages, warnings, excluded)
    for i, process in enumerate(processes):
        label = f"processes[{i}]"
        if not isinstance(process, dict):
            warnings.append(f"{label}: expected object")
            continue
        check_enums(label, "process", process, warnings)
        if not isinstance(process.get("body"), str) or not process["body"].strip():
            warnings.append(f"{label}: missing body")
    for field, group in (("participants", "participant"), ("grounds", "ground"),
                         ("remarks", "remark"), ("events", "event")):
        for i, item in enumerate(parsed.get(field, [])):
            label = f"{field}[{i}]"
            if not isinstance(item, dict):
                warnings.append(f"{label}: expected object")
                continue
            if type(item.get("p")) is not int or not 1 <= item["p"] <= len(processes):
                warnings.append(f"{label}: p must point to a process (1..{len(processes)})")
            elif version in {"v4.4", "v4.5", "v4.6", "v4.7", "v4.8", "v4.9", "v4.10"} and isinstance(processes[item["p"] - 1], dict):
                proc_dockets = processes[item["p"] - 1].get("dockets") or []
                named = item.get("dockets")
                if "docket" in item:
                    warnings.append(f"{label}: use dockets array, not legacy docket")
                if named is not None and (not isinstance(named, list) or
                                           any(not isinstance(d, str) for d in named)):
                    warnings.append(f"{label}: dockets must be an array of strings")
                elif isinstance(named, list):
                    allowed = {d for raw in proc_dockets for d in normalize_dockets(str(raw))}
                    for raw in named:
                        normalized = normalize_dockets(raw)
                        if not normalized or not set(normalized) <= allowed:
                            warnings.append(f"{label}: docket {raw!r} is not listed on its process")
                if group != "participant" and len(proc_dockets) > 1 and not named:
                    warnings.append(f"{label}: missing dockets for a multi-docket process")
            elif "docket" in item and isinstance(processes[item["p"] - 1], dict) and \
                    item["docket"] not in (processes[item["p"] - 1].get("dockets") or []):
                warnings.append(f"{label}: docket {item['docket']!r} is not listed on its process")
            elif version == "v4.3" and group != "participant" and "docket" not in item and \
                    isinstance(processes[item["p"] - 1], dict) and \
                    len(processes[item["p"] - 1].get("dockets") or []) > 1:
                warnings.append(f"{label}: missing docket for a multi-docket process")
            check_enums(label, group, item, warnings)
            if group == "event":
                check_date(label, item.get("date"), warnings)
                if "final" in item and not isinstance(item["final"], bool):
                    warnings.append(f"{label}: final must be true or false")
                if version in {"v4.7", "v4.8", "v4.9", "v4.10"} and item.get("kind") == "ruling":
                    if "final" not in item:
                        warnings.append(f"{label}: court ruling requires boolean final")
                    outcome = item.get("outcome")
                    if not outcome:
                        warnings.append(f"{label}: court ruling requires outcome")
                    elif item.get("final") is False and outcome not in ("interim", "remanded"):
                        warnings.append(f"{label}: nonfinal ruling outcome must be interim or remanded")
                    if item.get("final") is True and outcome == "interim":
                        warnings.append(f"{label}: final ruling cannot have interim outcome")
                    if outcome == "interim" and "mechanism" in item:
                        warnings.append(f"{label}: interim ruling must omit mechanism")
                for j, effect in enumerate(item.get("effects") or []):
                    if isinstance(effect, dict):
                        check_enums(f"{label}.effects[{j}]", "effect", effect, warnings)
            if group == "participant":
                roles = item.get("roles")
                if not isinstance(roles, list) or not roles or not set(roles) <= PARTICIPANT_ROLES:
                    warnings.append(f"{label}: roles must be a non-empty list of allowed roles")
            if group == "ground" and "applies" in item and not isinstance(item["applies"], bool):
                warnings.append(f"{label}: applies must be true or false")
            if group in {"ground", "remark"} and not item.get("raised_by" if group == "ground" else "target"):
                warnings.append(f"{label}: missing {'raised_by' if group == 'ground' else 'target'}")
            check_evidence(label, item.get("evidence"), pages, warnings, excluded)
    for i, value in enumerate(parsed.get("uncertainties", [])):
        if not isinstance(value, str):
            warnings.append(f"uncertainties[{i}]: expected string")
    for i, value in enumerate(parsed.get("suggestions", [])):
        if not isinstance(value, dict) or not isinstance(value.get("idea"), str):
            warnings.append(f"suggestions[{i}]: expected idea and why")
    return parsed, warnings


V5_ENUMS = {
    "decision": frozenset({"approved", "approved_with_conditions", "denied", "other", "unclear"}),
    "relationship": frozenset({"developer", "neighbor", "organization", "other", "unclear"}),
    "seeks": frozenset({"overturn_approval", "overturn_denial", "change_conditions", "other", "unclear"}),
    "representation": frozenset({"represented", "pro_se", "unclear"}),
    "three_way": frozenset({"yes", "no", "unclear"}),
    "central": frozenset({"yes", "no", "unclear", "not_applicable"}),
    "milestone": frozenset({"filing", "judgment", "further_appeal", "reopening", "remand", "resolution"}),
    "litigation": frozenset({"resolved", "ongoing", "unclear"}),
    "permission": frozenset({"upheld", "changed", "overturned", "denied", "unclear"}),
}
V5_EVIDENCE = {"doc": "positive_int", "page": "positive_int", "quote": "nonempty_text"}
V5_RELATED = {"docket": "nullable_nonempty_text", "relationship": "text", "evidence": [V5_EVIDENCE]}
V5_PROJECT = {"description": "nullable_text", "municipality": "nullable_text",
              "proposed_units": "nullable_count", "proposed_lots": "nullable_count",
              "evidence": [V5_EVIDENCE]}
V5_DECISION = {"description": "nullable_text", "date": "date", "outcome": V5_ENUMS["decision"],
               "evidence": [V5_EVIDENCE]}
V5_APPELLANT = {"name": "nonempty_text", "relationship": V5_ENUMS["relationship"],
                "seeks": V5_ENUMS["seeks"], "representation": V5_ENUMS["representation"],
                "evidence": [V5_EVIDENCE]}
V5_DISCRETION = {"involved": V5_ENUMS["three_way"], "description": "nullable_text",
                 "evidence": [V5_EVIDENCE], "central": V5_ENUMS["central"],
                 "central_evidence": [V5_EVIDENCE]}
V5_TIMELINE = {"kind": V5_ENUMS["milestone"], "date": "date", "court_or_body": "text",
               "dockets": ["nonempty_text"], "description": "text", "evidence": [V5_EVIDENCE]}
V5_STATUS = {"litigation": V5_ENUMS["litigation"], "permission": V5_ENUMS["permission"],
             "description": "nullable_text", "evidence": [V5_EVIDENCE]}
V5_RESTRICTION = {"description": "text", "start": "date", "end": "date",
                  "evidence": [V5_EVIDENCE]}
V5_HOUSING_CHANGE = {"description": "text", "units_before": "nullable_count",
                     "units_after": "nullable_count", "lots_before": "nullable_count",
                     "lots_after": "nullable_count", "evidence": [V5_EVIDENCE]}
V5_DOCKET = {"docket": "nullable_nonempty_text", "related_dockets": [V5_RELATED],
             "project": V5_PROJECT, "municipal_decision": V5_DECISION,
             "appellants": [V5_APPELLANT], "discretion": V5_DISCRETION,
             "timeline": [V5_TIMELINE], "observed_status": V5_STATUS,
             "restrictions": [V5_RESTRICTION], "housing_changes": [V5_HOUSING_CHANGE]}
V5_ROOT = {"schema_version": frozenset({"v5"}), "dockets": [V5_DOCKET],
           "scouting": {"uncertainties": ["text"],
                        "suggestions": [{"idea": "text", "why": "text"}]}}
V6_DOCKET = {**V5_DOCKET,
             "observed_status": {**V5_STATUS, "permission": frozenset(
                 {"allowed", "allowed_with_conditions", "denied", "unclear"})},
             "housing_changes": [{"description": "text", "evidence": [V5_EVIDENCE]}]}
V6_ROOT = {**V5_ROOT, "schema_version": frozenset({"v6"}), "dockets": [V6_DOCKET]}


def _validate_v5_shape(value: object, spec: object, label: str, errors: list[str]) -> None:
    """Validate types and exact keys without changing extracted facts."""
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            errors.append(f"{label}: expected object")
            return
        for key in sorted(spec.keys() - value.keys()):
            errors.append(f"{label}: missing {key}")
        for key in sorted(value.keys() - spec.keys()):
            errors.append(f"{label}: unexpected {key}")
        for key in spec.keys() & value.keys():
            _validate_v5_shape(value[key], spec[key], f"{label}.{key}", errors)
    elif isinstance(spec, list):
        if not isinstance(value, list):
            errors.append(f"{label}: expected array")
        else:
            for i, item in enumerate(value):
                _validate_v5_shape(item, spec[0], f"{label}[{i}]", errors)
    elif isinstance(spec, frozenset):
        if not isinstance(value, str) or value not in spec:
            errors.append(f"{label}: expected one of {', '.join(sorted(spec))}")
    elif spec == "date":
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
                errors.append(f"{label}: expected null or YYYY, YYYY-MM, YYYY-MM-DD")
            else:
                try:
                    datetime.strptime(value, {4: "%Y", 7: "%Y-%m", 10: "%Y-%m-%d"}[len(value)])
                except ValueError:
                    errors.append(f"{label}: invalid date")
    elif spec == "nullable_count":
        if value is not None and (type(value) is not int or value < 0):
            errors.append(f"{label}: expected null or nonnegative integer")
    elif spec == "positive_int":
        if type(value) is not int or value < 1:
            errors.append(f"{label}: expected positive integer")
    elif spec == "nullable_text":
        if value is not None and not isinstance(value, str):
            errors.append(f"{label}: expected string or null")
    elif spec == "nonempty_text":
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: expected nonempty string")
    elif spec == "nullable_nonempty_text":
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{label}: expected null or nonempty string")
    elif spec == "text":
        if not isinstance(value, str):
            errors.append(f"{label}: expected string")


def validate_focused_extract(raw: str, pages: dict[int, dict[int, str]],
                             excluded: set[int] = frozenset(),
                             version: str = "v5") -> tuple[dict | None, list[str]]:
    """Validate v5's one-record-per-appeal shape and warn on unsupported claims."""
    parsed, error = parse_object(raw)
    if error:
        return None, [error]
    errors: list[str] = []
    # Scouting feedback is not research data; its omission must not invalidate
    # otherwise complete docket records. If supplied, its shape is still checked.
    normalization = []
    # Feedback belongs outside records even if the model nests it in a docket.
    # Relocation preserves all feedback and changes no research field.
    root_feedback = parsed.get("scouting", {"uncertainties": [], "suggestions": []})
    feedback_errors = []
    _validate_v5_shape(root_feedback, V5_ROOT["scouting"], "scouting", feedback_errors)
    if not feedback_errors and isinstance(parsed.get("dockets"), list):
        for i, record in enumerate(parsed["dockets"]):
            if not isinstance(record, dict) or "scouting" not in record:
                continue
            nested_errors = []
            _validate_v5_shape(record["scouting"], V5_ROOT["scouting"], "scouting", nested_errors)
            if nested_errors:
                continue
            feedback = record.pop("scouting")
            for key in ("uncertainties", "suggestions"):
                root_feedback[key].extend(feedback[key])
            parsed["scouting"] = root_feedback
            normalization.append(f"dockets[{i}]: moved scouting feedback outside research record")
    root = V6_ROOT if version == "v6" else V5_ROOT
    spec = root if "scouting" in parsed else {k: v for k, v in root.items() if k != "scouting"}

    def remove_empty_extras(value, shape, label):
        # An unrequested null carries no fact. Keep raw output in the turn log,
        # report the normalization, and export only the requested schema.
        if isinstance(value, dict) and isinstance(shape, dict):
            for key in list(value):
                if key not in shape and value[key] is None:
                    del value[key]
                    normalization.append(f"{label}: removed unrequested null field {key}")
                elif key in shape:
                    remove_empty_extras(value[key], shape[key], f"{label}.{key}")
        elif isinstance(value, list) and isinstance(shape, list):
            for i, item in enumerate(value):
                remove_empty_extras(item, shape[0], f"{label}[{i}]")

    remove_empty_extras(parsed, spec, "extract")
    _validate_v5_shape(parsed, spec, "extract", errors)
    if errors:
        return None, errors
    warnings: list[str] = list(normalization)

    def cite(label: str, item: dict, substantive: bool = True, field: str = "evidence") -> None:
        evidence = item[field]
        if evidence or substantive:
            check_evidence(label, evidence, pages, warnings, excluded)

    seen = set()
    for i, docket in enumerate(parsed["dockets"]):
        label = f"dockets[{i}]"
        key = (tuple(normalize_dockets(docket["docket"])) or
               (docket["docket"].casefold(),)) if docket["docket"] is not None else None
        if key is not None and key in seen:
            warnings.append(f"{label}: repeated docket {docket['docket']!r}")
        if key is not None:
            seen.add(key)
        for j, related in enumerate(docket["related_dockets"]):
            cite(f"{label}.related_dockets[{j}]", related)
        project = docket["project"]
        cite(f"{label}.project", project,
             any(project[field] is not None for field in
                 ("description", "municipality", "proposed_units", "proposed_lots")))
        decision = docket["municipal_decision"]
        cite(f"{label}.municipal_decision", decision,
             decision["description"] is not None or decision["date"] is not None or
             decision["outcome"] != "unclear")
        for j, appellant in enumerate(docket["appellants"]):
            cite(f"{label}.appellants[{j}]", appellant)
        discretion = docket["discretion"]
        if discretion["involved"] == "no" and discretion["central"] != "not_applicable":
            warnings.append(f"{label}.discretion: central must be not_applicable when involved is no")
        if discretion["involved"] == "unclear" and discretion["central"] not in {"unclear", "not_applicable"}:
            warnings.append(f"{label}.discretion: centrality cannot be established when involvement is unclear")
        if discretion["involved"] == "yes" and discretion["central"] == "not_applicable":
            warnings.append(f"{label}.discretion: central cannot be not_applicable when involved is yes")
        cite(f"{label}.discretion", discretion,
             discretion["involved"] != "unclear" or discretion["description"] is not None)
        cite(f"{label}.discretion.central", discretion,
             discretion["central"] in {"yes", "no"}, "central_evidence")
        for j, item in enumerate(docket["timeline"]):
            cite(f"{label}.timeline[{j}]", item)
        status = docket["observed_status"]
        cite(f"{label}.observed_status", status,
             status["litigation"] != "unclear" or status["permission"] != "unclear" or
             status["description"] is not None)
        for field in ("restrictions", "housing_changes"):
            for j, item in enumerate(docket[field]):
                cite(f"{label}.{field}[{j}]", item)
        for j, change in enumerate(docket["housing_changes"] if version == "v5" else []):
            for measure in ("units", "lots"):
                before, after = change[f"{measure}_before"], change[f"{measure}_after"]
                proposed = project[f"proposed_{measure}"]
                change_label = f"{label}.housing_changes[{j}]"
                if before is not None and proposed is not None and before != proposed:
                    warnings.append(f"{change_label}: review {measure} baseline {before}; "
                                    f"proposal count is {proposed}, not the existing housing count")
                if before is not None and before == after:
                    warnings.append(f"{change_label}: unchanged {measure} count {before}; "
                                    "use null counts for changes to other attributes")
    return parsed, warnings


def extraction_counts(parsed: dict) -> dict[str, int]:
    """Item counts for either extraction schema."""
    if parsed.get("schema_version") in {"v5", "v6"}:
        dockets = parsed["dockets"]
        return {"dockets": len(dockets), **{field: sum(len(d[field]) for d in dockets)
                 for field in ("related_dockets", "appellants", "timeline", "restrictions",
                               "housing_changes")}}
    processes = [p for p in parsed.get("processes", []) if isinstance(p, dict)]
    names = ("events", "participants", "grounds", "remarks")
    if parsed.get("schema_version") in FLAT_VERSIONS:
        counts = {name: len(parsed.get(name) or []) for name in names}
    else:
        counts = {name: sum(len(p.get(name) or []) for p in processes) for name in names}
    counts["processes"] = len(processes)
    return counts


def cost_usd(usage: object) -> float | None:
    """Price at the recorded multiplier; historical turns retain base prices."""
    if isinstance(usage, dict) and "_reserved_cost_usd" in usage:
        return float(usage["_reserved_cost_usd"])
    counts = usage_counts(usage)
    if counts is None:
        return None
    prompt, cached, output, _ = counts
    hit = cached or 0
    multiplier = usage.get("_price_multiplier", 1.0)
    return multiplier * (hit * PRICES_PER_M["cache_hit"] + (prompt - hit) * PRICES_PER_M["cache_miss"]
                         + output * PRICES_PER_M["output"]) / 1_000_000


def request_cost_bound(request: dict, multiplier: float) -> float:
    """Conservative byte-per-input-token bound, all cache misses, capped output."""
    input_bound = sum(len(m["content"].encode("utf-8")) + 256 for m in request["messages"]) + 1024
    return multiplier * (input_bound * PRICES_PER_M["cache_miss"]
                         + request["max_tokens"] * PRICES_PER_M["output"]) / 1_000_000


def spent_usd(conn: duckdb.DuckDBPyConnection, since: str = "") -> float:
    """Spend on all group turns that made a request since an ISO timestamp."""
    rows = conn.execute("""
        SELECT usage_json FROM group_turns
        WHERE status NOT IN ('cached', 'skipped') AND attempted_at >= ?
    """, [since]).fetchall()
    return sum(cost_usd(json.loads(row[0])) or 0 for row in rows if row[0])


# ---------------------------------------------------------------------------
# Planning


def load_prompts(prompt_set: str = DEFAULT_PROMPT_SET) -> dict[str, str]:
    if prompt_set not in PROMPT_SETS:
        raise ValueError(f"Prompt set must be one of {', '.join(PROMPT_SETS)}")
    return {name: path.read_text(encoding="utf-8") for name, path in PROMPT_SETS[prompt_set].items()}


def requeued_groups(batch_id: str) -> list[dict]:
    """One single-document group per document that triage excluded from its group.

    The group ID is `<original group>~doc<N>`; the document keeps its caption and
    listing dockets but loses the membership basis, since it now stands alone.
    """
    groups = []
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        for group_id, documents, route in conn.execute("""
            SELECT m.group_id, m.documents_json, t.route_json
            FROM group_turns t JOIN group_batch_members m USING (batch_id, group_id)
            WHERE t.batch_id = ? AND t.step = 'triage' AND t.status IN ('ok', 'cached')
            ORDER BY m.ordinal, t.attempted_at
        """, [batch_id]).fetchall():
            excluded = json.loads(route or "{}").get("excluded_docs", [])
            for doc in json.loads(documents):
                if doc["doc"] in excluded and not any(g["group_id"] == f"{group_id}~doc{doc['doc']}" for g in groups):
                    alone = {**doc, "doc": 1, "basis": "requeued", "flags": [f"excluded_from:{group_id}"]}
                    groups.append({"group_id": f"{group_id}~doc{doc['doc']}",
                                   "dockets": doc["caption_dockets"] or doc["listing_dockets"],
                                   "composition": "requeued", "text_chars": doc["chars"],
                                   "documents": [alone]})
    return groups


def plan(args: argparse.Namespace) -> str:
    group_ids = list(args.group or [])
    notes = dict(note.split("=", 1) for note in (args.note or []))
    frozen = {}
    frozen_version = None
    if getattr(args, "from_batch", None):
        with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
            source = conn.execute("SELECT grouping_version FROM group_batches WHERE batch_id = ?",
                                  [args.from_batch]).fetchone()
            if source is None:
                raise ValueError(f"Unknown source batch: {args.from_batch}")
            frozen_version = source[0]
            for group_id, note, dockets, composition, documents, chars in conn.execute("""
                SELECT group_id, note, dockets, composition, documents_json, text_chars
                FROM group_batch_members WHERE batch_id = ? ORDER BY ordinal
            """, [args.from_batch]).fetchall():
                frozen[group_id] = {"group_id": group_id, "dockets": list(dockets),
                                    "composition": composition, "documents": json.loads(documents),
                                    "text_chars": chars}
                if group_id not in group_ids:
                    group_ids.append(group_id)
                notes.setdefault(group_id, note)
    requeued = [g for batch_id in (getattr(args, "requeue_excluded", None) or [])
                for g in requeued_groups(batch_id)]
    requeued = [g for i, g in enumerate(requeued)
                if g["group_id"] not in set(group_ids) | {x["group_id"] for x in requeued[:i]}]
    if not group_ids and not requeued:
        raise ValueError("Name groups with --group (see groups.py status/show)")
    if len(group_ids) + len(requeued) > MAX_BATCH_GROUPS or len(set(group_ids)) != len(group_ids):
        raise ValueError(f"Use 1..{MAX_BATCH_GROUPS} distinct groups")
    for level in (args.triage_reasoning, args.extract_reasoning):
        if level is not None and level not in REASONING_LEVELS:
            raise ValueError(f"Reasoning must be one of {', '.join(REASONING_LEVELS)}")
    prompts = load_prompts(getattr(args, "prompt_set", None) or DEFAULT_PROMPT_SET)
    with duckdb.connect(str(GROUPS_DB_PATH), read_only=True) as groups_db:
        version = groups_db.execute("SELECT version FROM grouping_meta").fetchone()[0]
        if frozen_version is not None:
            if (any(gid not in frozen for gid in group_ids) or requeued) and version != frozen_version:
                raise ValueError("Cannot mix frozen and current grouping versions in one batch")
            version = frozen_version
        groups = [frozen[group_id] if group_id in frozen else load_group(groups_db, group_id)
                  for group_id in group_ids] + requeued
    if getattr(args, "only_group", None):
        selected = set(args.only_group)
        if selected - {g["group_id"] for g in groups}:
            raise ValueError("--only-group must name groups present in the planned batch")
        groups = [g for g in groups if g["group_id"] in selected]
    batch_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-g{uuid.uuid4().hex[:7]}"
    SCOUT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(SCOUT_DB_PATH)) as conn:
        init_db(conn)
        conn.execute("INSERT INTO group_batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [batch_id, utc_now(), args.label, version,
                      digest(canonical_json(prompts)), prompts["system"], prompts["triage"],
                      prompts["extract"], args.triage_reasoning, args.extract_reasoning,
                      args.max_chars, len(groups)])
        for ordinal, group in enumerate(groups, 1):
            conn.execute("INSERT INTO group_batch_members VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [batch_id, ordinal, group["group_id"], group["dockets"],
                          group["composition"], canonical_json(group["documents"]),
                          group["text_chars"], notes.get(group["group_id"])])
    total = sum(g["text_chars"] for g in groups)
    print(f"Group batch {batch_id} ({schema_of(prompts['extract'])}): {len(groups)} groups, "
          f"{sum(len(g['documents']) for g in groups)} PDFs, {total:,} characters; "
          f"reasoning triage={args.triage_reasoning or 'default'} "
          f"extract={args.extract_reasoning or 'default'}")
    for group in groups:
        size = "  TOO LARGE" if group["text_chars"] > args.max_chars else ""
        print(f"  {group['group_id']}: {len(group['documents'])} PDFs, "
              f"{group['text_chars']:,} chars, {group['composition']}{size}")
    return batch_id


# ---------------------------------------------------------------------------
# Running


def usage_text(usage: object) -> str:
    counts = usage_counts(usage)
    if counts is None:
        return "no usage reported"
    prompt, cached, output, _ = counts
    reasoning = reasoning_tokens(usage)
    cache = f"{cached:,} cached" if cached is not None else "cache not reported"
    thought = f", {reasoning:,} reasoning" if reasoning is not None else ""
    return f"input {prompt:,} ({cache}), output {output:,}{thought}"


def step_summary(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[str]:
    lines = []
    for step in ("triage", "extract"):
        rows = conn.execute("""
            SELECT usage_json FROM group_turns
            WHERE run_id = ? AND step = ? AND status NOT IN ('cached', 'skipped')
        """, [run_id, step]).fetchall()
        usages = [json.loads(row[0]) if row[0] else None for row in rows]
        counted = [(usage_counts(u), reasoning_tokens(u)) for u in usages]
        known = [c for c in counted if c[0] is not None]
        if not rows:
            lines.append(f"  {step}: no new requests")
            continue
        prompt = sum(c[0][0] for c in known)
        cached = sum(c[0][1] or 0 for c in known)
        output = sum(c[0][2] for c in known)
        reasoning = sum(c[1] or 0 for c in known)
        share = f" ({cached / prompt:.0%} of input)" if prompt else ""
        dollars = sum(cost_usd(u) or 0 for u in usages)
        lines.append(f"  {step}: {len(rows)} requests; input {prompt:,}, cached {cached:,}{share}, "
                     f"output {output:,} (reasoning {reasoning:,}); ${dollars:.4f}; "
                     f"{len(rows) - len(known)} without usage")
    return lines


def run(args: argparse.Namespace) -> None:
    endpoint, model, key, _ = endpoint_from_env()
    workers = max(1, min(getattr(args, "workers", 1), 5))
    price_multiplier = getattr(args, "price_multiplier", 1.0)
    max_output = getattr(args, "max_output_tokens", 16384)
    if not 0 < price_multiplier <= 100 or not 1 <= max_output <= 65536:
        raise ValueError("Use a positive price multiplier and 1..65536 max output tokens")
    with duckdb.connect(str(SCOUT_DB_PATH)) as conn:
        init_db(conn)
        batch = conn.execute("""
            SELECT system_prompt, triage_prompt, extract_prompt, triage_reasoning,
                   extract_reasoning, max_chars
            FROM group_batches WHERE batch_id = ?
        """, [args.batch_id]).fetchone()
        if not batch:
            raise ValueError(f"Unknown group batch: {args.batch_id}")
        prompts = {"system": batch[0], "triage": batch[1], "extract": batch[2]}
        versions = {step: schema_of(prompts[step]) for step in ("triage", "extract")}
        levels = {"triage": batch[3], "extract": batch[4]}
        max_chars = batch[5]
        members = conn.execute("""
            SELECT ordinal, group_id, documents_json, text_chars FROM group_batch_members
            WHERE batch_id = ? ORDER BY ordinal
        """, [args.batch_id]).fetchall()
        run_id = uuid.uuid4().hex
        conn.execute("INSERT INTO group_runs VALUES (?, ?, ?, NULL, ?, ?, 0, 0, 0)",
                     [run_id, args.batch_id, utc_now(), model, endpoint])
        counts = {"requested": 0, "cached": 0, "failed": 0}
        routes: dict[str, str] = {}

        def record(job: dict, step: str, status: str, http_status: int | None, request: dict,
                   raw: str | None, parsed: dict | None, warnings: list[str], usage: object,
                   finish: str | None, elapsed: float, error: str | None,
                   route: dict | None = None, reused: str | None = None) -> None:
            request_json = canonical_json(request)
            conn.execute("""
                INSERT INTO group_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                                ?, ?, ?, ?, ?, ?, ?, ?)
            """, [uuid.uuid4().hex, args.batch_id, job["group_id"], step, run_id, now_precise(),
                  digest(request_json), digest(job["text"]), model, endpoint, levels[step],
                  status, http_status, request_json, raw,
                  canonical_json(parsed) if parsed is not None else None,
                  canonical_json(warnings), canonical_json(usage) if usage else None,
                  finish, elapsed, error, route["route"] if route else None,
                  canonical_json(route) if route else None, reused])

        def prior(request: dict) -> tuple | None:
            return conn.execute("""
                SELECT turn_id, response_text, parsed_json, validation_warnings, usage_json,
                       finish_reason, route_json, http_status
                FROM group_turns WHERE request_sha256 = ? AND status = 'ok'
                ORDER BY attempted_at DESC LIMIT 1
            """, [digest(canonical_json(request))]).fetchone()

        def uncertain(request: dict) -> bool:
            return conn.execute("""
                SELECT 1 FROM group_turns WHERE request_sha256 = ?
                AND status = 'uncertain_delivery' LIMIT 1
            """, [digest(canonical_json(request))]).fetchone() is not None

        jobs = []
        for ordinal, group_id, documents_json, chars in members:
            documents = json.loads(documents_json)
            job = {"ordinal": ordinal, "group_id": group_id, "documents": documents,
                   "chars": chars, "tag": f"[{ordinal}/{len(members)}] {group_id}"}
            if chars > max_chars:
                print(f"{job['tag']}: {chars:,} characters exceeds {max_chars:,}; skipped")
                routes[group_id] = "too_large"
                job["text"] = ""
                record(job, "triage", "skipped", None, {}, None, None, [], None, None, 0.0,
                       "Group exceeds the batch character limit",
                       {"route": "too_large", "excluded_docs": []})
                continue
            job["text"], job["pages"] = render_group(group_id, documents)
            jobs.append(job)

        def after_triage(job: dict, parsed: dict) -> dict:
            route = route_triage(parsed, [d["doc"] for d in job["documents"]])
            routes[job["group_id"]] = route["route"]
            excluded = f"; excluded docs {route['excluded_docs']}" if route["excluded_docs"] else ""
            print(f"  {job['group_id']} triage: municipal_appeal={route['municipal_appeal']} "
                  f"housing={route['housing']} -> {route['route']}{excluded}")
            matter = parsed.get("matter")
            if matter:
                print(f"    {matter}")
            return route

        def next_request(job: dict) -> tuple[str, dict] | None:
            """Advance a group through cached turns; return the next request to send."""
            triage = triage_payload(model, prompts, job["text"], levels["triage"])
            triage["max_tokens"] = max_output
            if "triage_raw" not in job:
                found = prior(triage)
                if not found:
                    return ("triage", triage)
                counts["cached"] += 1
                parsed = json.loads(found[2])
                route = after_triage(job, parsed)
                record(job, "triage", "cached", found[7], triage, found[1], parsed,
                       json.loads(found[3] or "[]"), json.loads(found[4]) if found[4] else None,
                       found[5], 0.0, None, route, found[0])
                print(f"{job['tag']} triage cached")
                job["triage_raw"], job["route"] = found[1], route
            if job["route"]["route"] != "extract" or getattr(args, "triage_only", False):
                return None
            extract = extract_payload(triage, job["triage_raw"], prompts, levels["extract"])
            found = prior(extract)
            if not found:
                return ("extract", extract)
            counts["cached"] += 1
            record(job, "extract", "cached", found[7], extract, found[1],
                   json.loads(found[2]), json.loads(found[3] or "[]"),
                   json.loads(found[4]) if found[4] else None, found[5], 0.0, None, None, found[0])
            print(f"{job['tag']} extract cached")
            return None

        halt = False
        next_launch = 0.0
        max_spend = getattr(args, "max_spend", None)
        spend_since = getattr(args, "spend_since", "") or ""
        if max_spend is not None:
            print(f"Spend since {spend_since or 'the beginning'}: "
                  f"${spent_usd(conn, spend_since):.4f} of ${max_spend:.2f}")
        queue = list(jobs)
        pending = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while queue or pending:
                while not halt and queue and len(pending) < workers:
                    job = queue.pop(0)
                    step_request = next_request(job)
                    if step_request is None:
                        continue
                    step, request = step_request
                    reservation = request_cost_bound(request, price_multiplier)
                    in_flight = sum(request_cost_bound(item[2], price_multiplier) for item in pending.values())
                    if max_spend is not None and spent_usd(conn, spend_since) + in_flight + reservation > max_spend:
                        print(f"Stopping before {job['group_id']}: ${reservation:.4f} request bound "
                              f"does not fit remaining ${max_spend-spent_usd(conn, spend_since)-in_flight:.4f}.", file=sys.stderr)
                        halt = True
                        break
                    if uncertain(request) and not args.retry_uncertain:
                        print(f"{job['tag']} {step}: earlier delivery uncertain; skipped "
                              "(use --retry-uncertain to resend)", file=sys.stderr)
                        counts["failed"] += 1
                        continue
                    gap = next_launch - time.monotonic()
                    if gap > 0:
                        time.sleep(gap)
                    print(f"{job['tag']} {step} requesting "
                          f"({len(job['documents'])} PDFs, {len(job['text']):,} chars, "
                          f"reasoning {levels[step] or 'default'})", flush=True)
                    future = pool.submit(request_completion, endpoint, key, request, args.timeout)
                    pending[future] = (job, step, request)
                    counts["requested"] += 1
                    next_launch = time.monotonic() + args.delay
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    job, step, request = pending.pop(future)
                    try:
                        http_status, response, error, elapsed = future.result()
                    except Exception as exc:
                        http_status, response, error, elapsed = (
                            0, None, f"Delivery uncertain after worker error: {type(exc).__name__}", 0.0)
                    choice = (response or {}).get("choices", [{}]) if isinstance(response, dict) else [{}]
                    choice = choice[0] if isinstance(choice, list) and choice else {}
                    message = choice.get("message") if isinstance(choice, dict) else None
                    raw = message.get("content") if isinstance(message, dict) else None
                    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
                    usage = response.get("usage") if isinstance(response, dict) else None
                    if isinstance(usage, dict):
                        usage = {**usage, "_price_multiplier": price_multiplier}
                    if usage_counts(usage) is None and (http_status == 200 or (http_status or 0) >= 500
                                                       or (error or "").startswith("Delivery uncertain")):
                        usage = {"_reserved_cost_usd": request_cost_bound(request, price_multiplier),
                                 "_price_multiplier": price_multiplier}
                        halt = True  # do not spend further against an unknown bill
                    parsed, warnings, route = None, [], None
                    if error is None:
                        if not isinstance(raw, str) or not raw.strip():
                            error = "Missing message content"
                        elif finish == "length":
                            error = "Model response reached output limit"
                        elif finish == "content_filter":
                            error = "Model response stopped by content filter"
                        elif step == "triage":
                            parsed, warnings = validate_triage(raw, job["pages"], versions["triage"])
                        else:
                            excluded = set(job["route"]["excluded_docs"])
                            parsed, warnings = validate_extract(raw, job["pages"], excluded,
                                                                versions["extract"])
                        if error is None and parsed is None:
                            error = "; ".join(warnings)
                    if error is None:
                        status = "ok"
                    elif error.startswith("Delivery uncertain") or (http_status or 0) >= 500:
                        status = "uncertain_delivery"
                    elif http_status == 200:
                        status = "invalid_output"
                    else:
                        status = "api_error"
                    if status == "ok" and step == "triage":
                        route = after_triage(job, parsed)
                    record(job, step, status, http_status, request, raw, parsed, warnings,
                           usage, finish, elapsed, error, route)
                    print(f"{job['tag']} {step} {status}; {usage_text(usage)}; "
                          f"{len(warnings)} warnings; {elapsed:.1f}s", flush=True)
                    if status != "ok":
                        counts["failed"] += 1
                        print(f"  error: {error}", file=sys.stderr)
                        if status == "uncertain_delivery" or http_status in HALT_STATUSES or not http_status:
                            halt = True
                            print("Stopping new requests; saving responses already in flight.",
                                  file=sys.stderr)
                        continue
                    if step == "triage":
                        job["triage_raw"], job["route"] = raw, route
                        if route["route"] == "extract" and not getattr(args, "triage_only", False):
                            queue.insert(0, job)
                    else:
                        tally = extraction_counts(parsed)
                        print(f"  {job['group_id']} extract: "
                              + ", ".join(f"{v} {k}" for k, v in tally.items()))
        conn.execute("""
            UPDATE group_runs SET finished_at = ?, requested_count = ?, cached_count = ?,
                                  failed_count = ? WHERE run_id = ?
        """, [utc_now(), counts["requested"], counts["cached"], counts["failed"], run_id])
        print(f"\nRun {run_id}: {counts['requested']} requests, {counts['cached']} cached turns, "
              f"{counts['failed']} failures")
        for line in step_summary(conn, run_id):
            print(line)
        if max_spend is not None:
            print(f"Spend since {spend_since or 'the beginning'}: "
                  f"${spent_usd(conn, spend_since):.4f} of ${max_spend:.2f}")
        tally: dict[str, int] = {}
        for value in routes.values():
            tally[value] = tally.get(value, 0) + 1
        print("Routes: " + (", ".join(f"{k} {v}" for k, v in sorted(tally.items())) or "none"))
    if counts["failed"]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Reporting


def latest_turns(conn: duckdb.DuckDBPyConnection, batch_id: str) -> dict[tuple[str, str], tuple]:
    rows = conn.execute("""
        SELECT group_id, step, status, route, usage_json, validation_warnings, error, attempted_at
        FROM group_turns WHERE batch_id = ? ORDER BY attempted_at, turn_id
    """, [batch_id]).fetchall()
    latest: dict[tuple[str, str], tuple] = {}
    for row in rows:
        previous = latest.get((row[0], row[1]))
        if row[2] == "cached" and previous and previous[2] == "ok":
            continue  # Reuse of an earlier success: report the original turn.
        latest[(row[0], row[1])] = row
    return latest


def status(args: argparse.Namespace) -> None:
    if not SCOUT_DB_PATH.exists():
        print("No scouting database yet")
        return
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        if "group_batches" not in tables:
            print("No group batches yet")
            return
        batches = conn.execute("""
            SELECT batch_id, created_at, label, group_count, triage_reasoning, extract_reasoning
            FROM group_batches ORDER BY created_at DESC
        """).fetchall()
        for batch_id, created, label, count, tr, er in batches:
            turns = latest_turns(conn, batch_id)
            done = sum(1 for (g, s), row in turns.items() if s == "triage" and row[2] in {"ok", "cached", "skipped"})
            extracted = sum(1 for (g, s), row in turns.items() if s == "extract" and row[2] in {"ok", "cached"})
            print(f"{batch_id} {created[:16]} {label or ''}: {count} groups; triaged {done}, "
                  f"extracted {extracted}; reasoning {tr or 'default'}/{er or 'default'}")


def report(args: argparse.Namespace) -> None:
    with duckdb.connect(str(SCOUT_DB_PATH), read_only=True) as conn:
        members = conn.execute("""
            SELECT ordinal, group_id, composition, text_chars, note FROM group_batch_members
            WHERE batch_id = ? ORDER BY ordinal
        """, [args.batch_id]).fetchall()
        if not members:
            raise ValueError(f"Unknown group batch: {args.batch_id}")
        turns = latest_turns(conn, args.batch_id)
        for ordinal, group_id, composition, chars, note in members:
            print(f"{ordinal}. {group_id} ({composition}, {chars:,} chars){' — ' + note if note else ''}")
            for step in ("triage", "extract"):
                row = turns.get((group_id, step))
                if row is None:
                    print(f"   {step}: not run")
                    continue
                warnings = json.loads(row[5] or "[]")
                route = f" -> {row[3]}" if row[3] else ""
                usage = json.loads(row[4]) if row[4] else None
                dollars = f"${cost_usd(usage):.4f}; " if usage and cost_usd(usage) is not None else ""
                print(f"   {step}: {row[2]}{route}; {usage_text(usage)}; {dollars}"
                      f"{len(warnings)} warnings{'; ' + row[6] if row[6] else ''}")


def revalidate(args: argparse.Namespace) -> None:
    """Recompute stored warnings for successful turns after a validator change."""
    with duckdb.connect(str(SCOUT_DB_PATH)) as conn:
        members = {group_id: json.loads(documents) for group_id, documents in conn.execute("""
            SELECT group_id, documents_json FROM group_batch_members WHERE batch_id = ?
        """, [args.batch_id]).fetchall()}
        if not members:
            raise ValueError(f"Unknown group batch: {args.batch_id}")
        prompts = conn.execute("""
            SELECT triage_prompt, extract_prompt FROM group_batches WHERE batch_id = ?
        """, [args.batch_id]).fetchone()
        versions = {"triage": schema_of(prompts[0]), "extract": schema_of(prompts[1])}
        turns = conn.execute("""
            SELECT turn_id, group_id, step, response_text, route_json, validation_warnings, status
            FROM group_turns WHERE batch_id = ?
            AND (status IN ('ok', 'cached') OR (status = 'invalid_output' AND response_text IS NOT NULL))
        """, [args.batch_id]).fetchall()
        routes = {group_id: json.loads(route) for _, group_id, step, _, route, _, _ in turns
                  if step == "triage" and route}
        pages = {}
        for turn_id, group_id, step, raw, _, old, status in turns:
            if group_id not in pages:
                pages[group_id] = render_group(group_id, members[group_id])[1]
            if step == "triage":
                parsed, warnings = validate_triage(raw, pages[group_id], versions["triage"])
            else:
                excluded = set((routes.get(group_id) or {}).get("excluded_docs", []))
                parsed, warnings = validate_extract(raw, pages[group_id], excluded, versions["extract"])
            if status == "invalid_output":
                if parsed is None or step == "triage":
                    continue  # A recovered triage would also need routing; rerun instead.
                # The stored response now validates (e.g. after JSON repair): no new request.
                conn.execute("""UPDATE group_turns SET status = 'ok', error = NULL, parsed_json = ?,
                                validation_warnings = ? WHERE turn_id = ?""",
                             [canonical_json(parsed), canonical_json(warnings), turn_id])
                print(f"{group_id} {step}: recovered stored invalid output; {len(warnings)} warnings")
                continue
            conn.execute("UPDATE group_turns SET validation_warnings = ?, parsed_json = ? WHERE turn_id = ?",
                         [canonical_json(warnings),
                          canonical_json(parsed) if parsed is not None else None, turn_id])
            print(f"{group_id} {step}: {len(json.loads(old or '[]'))} -> {len(warnings)} warnings")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    planning = sub.add_parser("plan", help="freeze a batch of docket groups")
    planning.add_argument("--group", action="append", help="group ID; repeat for several")
    planning.add_argument("--note", action="append", help="GROUP_ID=why it was selected")
    planning.add_argument("--from-batch", help="also include every group (and note) of this batch")
    planning.add_argument("--only-group", action="append", help="retain only these frozen group IDs; repeat to select a subset")
    planning.add_argument("--requeue-excluded", metavar="BATCH_ID", action="append",
                          help="add a single-document group for each document triage excluded "
                               "in BATCH_ID; repeat for several batches")
    planning.add_argument("--label", help="short batch label")
    planning.add_argument("--triage-reasoning", choices=REASONING_LEVELS)
    planning.add_argument("--extract-reasoning", choices=REASONING_LEVELS)
    planning.add_argument("--prompt-set", choices=sorted(PROMPT_SETS), default=DEFAULT_PROMPT_SET)
    planning.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                          help="skip groups with more selected-text characters")
    running = sub.add_parser("run", help="run or resume a group batch")
    running.add_argument("--batch-id", required=True)
    running.add_argument("--workers", type=int, default=1)
    running.add_argument("--timeout", type=float, default=650.0,
                         help="seconds per request; above the proxy's 600-second upstream limit")
    running.add_argument("--delay", type=float, default=0.5)
    running.add_argument("--retry-uncertain", action="store_true")
    running.add_argument("--triage-only", action="store_true",
                         help="run or reuse triage but do not extract")
    running.add_argument("--max-spend", type=float,
                         help="stop launching requests once priced spend reaches this many USD")
    running.add_argument("--spend-since", default="",
                         help="ISO timestamp from which --max-spend counts group turns")
    running.add_argument("--price-multiplier", type=float, default=1.0,
                         help="record actual pricing multiplier on new responses (2 for peak)")
    running.add_argument("--max-output-tokens", type=int, default=65536,
                         help="cap output for conservative pre-request budget reservations")
    sub.add_parser("status", help="list group batches")
    rechecking = sub.add_parser("revalidate", help="recompute warnings of stored successful turns")
    rechecking.add_argument("--batch-id", required=True)
    reporting = sub.add_parser("report", help="per-group turns, routes, and tokens")
    reporting.add_argument("--batch-id", required=True)
    args = parser.parse_args()
    try:
        {"plan": plan, "run": run, "status": status, "report": report,
         "revalidate": revalidate}[args.command](args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
