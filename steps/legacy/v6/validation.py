"""Validation of the September pipeline's answers: v5 triage and v6 docket records.

Copied unchanged in behavior from the scouting code the September run used
(``group_scout.py``). Citations are checked against the case's pages and moved
when a quote is uniquely found elsewhere; repairs and corrections are reported
as warnings. Only the v5-triage and v6 schemas are kept.
"""
from datetime import datetime
import json
import re

from court.contracts.quotes import quote_on_page
from court.core.json_repair import repair_json

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

TRIAGE_ENUMS = {"belongs": {"yes", "no", "unclear"},
                "scope": {"yes", "no", "unclear"},
                "case_type": {"municipal_permit_appeal", "municipal_enforcement", "act_250",
                              "state_permit", "state_enforcement", "bylaw_or_policy", "other",
                              "unclear"},
                "development": {"new_housing", "existing_home", "commercial", "industrial",
                                "institutional", "infrastructure", "agricultural",
                                "mixed_with_housing", "other", "unclear"}}

DOCKET_RE = re.compile(
    r"\b(?:(\d{1,3})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{2})\s*-?\s*V\s*[a-z]{2,3}\b"
    r"|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{2,6})\b"
    r"|eo\s*(\d{2})-(\d{2,4})\b)", re.I)


def normalize_match(m: re.Match) -> str:
    if m[1]:
        return f"{int(m[1])}-{int(m[2])}-{m[3]} Vtec"
    if m[4]:
        return f"{m[4]}-{m[5].upper()}-{int(m[6]):05d}"
    return f"eo{m[7]}-{int(m[8]):03d}"


def normalize_dockets(text: str | None) -> list[str]:
    """All docket-shaped strings in text, normalized, in first-seen order."""
    seen: dict[str, None] = {}
    for m in DOCKET_RE.finditer(text or ""):
        seen.setdefault(normalize_match(m))
    return list(seen)


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


def validate_extract(raw: str, pages: dict[int, dict[int, str]],
                     excluded: set[int] = frozenset(),
                     version: str = "v6") -> tuple[dict | None, list[str]]:
    if version not in {"v5", "v6"}:
        raise ValueError("Only the v5 and v6 extraction schemas are supported")
    raw, repairs = parse_output(raw, version)
    parsed, warnings = validate_focused_extract(raw, pages, excluded, version)
    return parsed, (repairs + warnings) if parsed is not None else warnings
