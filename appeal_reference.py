"""Bounded, source-bound research reference labels; no extraction or model calls."""
import json
import math
import re
from pathlib import Path

REFERENCE_VERSION = 'appeal-reference-1'
STATES = {'adjudicated', 'pending', 'disputed', 'unknown'}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json(value):
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json(item) for item in value)
    return isinstance(value, dict) and all(isinstance(key, str) and _json(item) for key, item in value.items())


def _fields(fields, context):
    _require(isinstance(fields, list), context + ': fields must be a list')
    seen = set()
    for field in fields:
        _require(isinstance(field, dict), context + ': expectation must be an object')
        path = field.get('path')
        _require(isinstance(path, list) and path and all(_text(key) for key in path), context + ': path must contain nonempty keys')
        _require(tuple(path) not in seen, context + ': duplicate field path')
        seen.add(tuple(path))
        state = field.get('state')
        _require(isinstance(state, str) and state in STATES, context + ': invalid label state')
        _require(isinstance(field.get('comparison'), str) and field['comparison'] in {'exact', 'unordered', 'prose'}, context + ': invalid comparison')
        _require(isinstance(field.get('evidence'), list) and _json(field['evidence']), context + ': evidence must be a JSON list')
        _require(isinstance(field.get('rationale'), str) and isinstance(field.get('version'), str), context + ': rationale and version must be strings')
        if state == 'adjudicated':
            _require('value' in field and _json(field['value']), context + ': adjudicated value must be JSON')
            _require(bool(field['evidence']) and _text(field['rationale']) and _text(field['version']), context + ': adjudicated labels need evidence, rationale, and version')
            if field['comparison'] == 'unordered':
                _require(isinstance(field['value'], list), context + ': unordered value must be a list')
        elif 'value' in field:
            _require(_json(field['value']), context + ': value must be JSON')


def validate_reference(value):
    """Return the supplied reference unchanged, or raise ``ValueError``.

    Unknown, pending and disputed labels remain explicit unscored states.
    Validation never interprets them as a missing extraction value.
    """
    _require(isinstance(value, dict), 'Reference must be an object')
    _require(value.get('schema_version') == REFERENCE_VERSION, 'Unsupported reference schema')
    _require(_text(value.get('reference_id')) and _text(value.get('version')), 'Reference identity and version required')
    _require(isinstance(value.get('purpose'), str) and value['purpose'] in {'synthetic_software_test', 'research_reference'}, 'Invalid reference purpose')
    _require(isinstance(value.get('cases'), list), 'Cases must be a list')
    case_ids = set()
    for case in value['cases']:
        _require(isinstance(case, dict), 'Case must be an object')
        cid = case.get('case_id')
        _require(_text(cid) and cid not in case_ids, 'Invalid or duplicate case identity')
        case_ids.add(cid)
        _require(isinstance(case.get('source_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', case['source_sha256']) is not None, cid + ': invalid source hash')
        adjudication = case.get('adjudication')
        _require(isinstance(adjudication, dict) and isinstance(adjudication.get('state'), str) and adjudication['state'] in STATES, cid + ': invalid adjudication state')
        _require(isinstance(adjudication.get('rationale'), str), cid + ': adjudication rationale required')
        if adjudication['state'] == 'adjudicated':
            _require(_text(adjudication['rationale']), cid + ': adjudication rationale must be nonempty')
        _require(isinstance(case.get('records_complete'), bool), cid + ': records_complete must be boolean')
        _fields(case.get('fields'), cid)
        _require(isinstance(case.get('records'), list), cid + ': records must be a list')
        record_ids = set()
        for record in case['records']:
            _require(isinstance(record, dict), cid + ': record must be an object')
            rid = record.get('record_id')
            _require(_text(rid) and rid not in record_ids, cid + ': invalid or duplicate record identity')
            record_ids.add(rid)
            identity = record.get('identity')
            _require(isinstance(identity, dict) and 'docket' in identity and (identity['docket'] is None or _text(identity['docket'])), cid + ': docket identity must be a string or null')
            _fields(record.get('fields'), cid + '/' + rid)
    return value


def read_reference(path):
    """Read and validate one JSON reference file without changing it."""
    return validate_reference(json.loads(Path(path).read_text(encoding='utf-8')))
