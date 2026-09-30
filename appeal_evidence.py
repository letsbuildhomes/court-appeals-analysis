"""Stable evidence validation and duration arithmetic for municipal appeals."""
import copy
import json
import re
from datetime import date
import group_scout as gs


def strip_citation_metadata(value):
    """Page-correction provenance is logged in warnings, not the research schema."""
    if isinstance(value, dict):
        if {'doc', 'page', 'quote'} <= set(value):
            value.pop('cited_as', None)
        for child in value.values():
            strip_citation_metadata(child)
    elif isinstance(value, list):
        for child in value:
            strip_citation_metadata(child)
    return value


FIELDS = {"project", "municipal_decision", "appellants", "discretion", "timeline",
          "observed_status", "restrictions", "housing_changes", "related_dockets"}

def source_pages(source):
    return {int(doc): {int(page): text for page, text in re.findall(
        r'<page number="(\d+)"[^>]*>(.*?)</page>', body, re.S)}
        for doc, body in re.findall(r'<document doc="(\d+)"[^>]*>(.*?)</document>', source, re.S)}

def apply_review(original, review, pages):
    """Validate a complete patch transaction; reject it atomically on failure."""
    review = copy.deepcopy(review)
    if review.get("schema_version") != "v6-review-1":
        raise ValueError("Wrong review schema")
    scope = review.get("scope", {})
    if scope.get("verdict") not in {"agree", "flag"}:
        raise ValueError("Invalid scope verdict")
    if not isinstance(review.get("changes"), list) or not isinstance(review.get("flags"), list):
        raise ValueError("changes and flags must be lists")
    if scope["verdict"] == "flag":
        return None, ["Scope flagged; original retained for manual review"]
    candidate = copy.deepcopy(original)
    seen = set()
    for change in review["changes"]:
        key = (change.get("docket"), change.get("field"))
        if key in seen or key[1] not in FIELDS or change.get("severity") not in {"material", "minor"}:
            raise ValueError("Duplicate or invalid change target/severity")
        seen.add(key)
        evidence_warnings = []
        gs.check_evidence("change", change.get("evidence"), pages, evidence_warnings)
        if evidence_warnings:
            raise ValueError("Unsupported change evidence: " + "; ".join(evidence_warnings))
        records = [d for d in (candidate or {}).get("dockets", []) if d["docket"] == key[0]]
        if len(records) != 1:
            raise ValueError("Change must identify exactly one existing docket")
        records[0][key[1]] = change["replacement"]
    if candidate is None:
        return None, []
    strip_citation_metadata(candidate)
    parsed, warnings = gs.validate_extract(json.dumps(candidate), pages, version="v6")
    if parsed is None:
        raise ValueError("Invalid corrected extraction: " + "; ".join(warnings))
    return strip_citation_metadata(parsed), warnings

def interval(record, pages):
    """Reject invalid endpoint evidence rather than turn it into a numeric claim."""
    warnings = []
    clean = copy.deepcopy(record)
    for side, allowed in [('start', {'exact', 'on_or_before'}), ('end', {'exact', 'on_or_after'})]:
        point = clean.get(side)
        if point is None:
            continue
        problems = []
        if not isinstance(point, dict) or set(point) != {'date','relation','description','evidence'}:
            problems.append('Malformed endpoint')
        else:
            try:
                parsed = date.fromisoformat(point['date'])
                if parsed.isoformat() != point['date']:
                    raise ValueError('Noncanonical date')
            except (ValueError, TypeError):
                problems.append('Invalid complete date')
            if point['relation'] not in allowed:
                problems.append('Invalid bound direction')
            gs.check_evidence(side, point['evidence'], pages, problems)
        if problems:
            clean[side] = None
            warnings.extend(f'{side}: {p}' for p in problems)
    start, end = clean.get('start'), clean.get('end')
    measure, days = 'unknown', None
    if start and end:
        days = (date.fromisoformat(end['date'])-date.fromisoformat(start['date'])).days
        if days < 0:
            warnings.append('Reversed interval; numeric result suppressed')
            days = None
        else:
            measure = 'exact' if start['relation'] == end['relation'] == 'exact' else 'at_least'
            if days == 0 and measure == 'at_least':
                warnings.append('Only a zero-day lower bound; no informative duration established')
                measure, days = 'unknown', None
    clean['duration'] = {'kind': measure, 'days': days}
    return clean, warnings
