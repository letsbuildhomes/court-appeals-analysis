"""Compare frozen extractions against the same adjudicated reference labels."""
import json
from collections import Counter, defaultdict


def _cases(value):
    value = value.get('canonical', value)
    if value.get('schema_version') != 'appeal-cases-1':
        raise ValueError('Unsupported canonical cases version')
    cases = value['cases']
    ids = [c['case_id'] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate canonical case_id')
    return {c['case_id']: c for c in cases}


def _identity(docket):
    return ' '.join(docket.strip().casefold().split()) if isinstance(docket, str) else None


def _lookup(obj, path):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return {'present': False, 'value': None}
        obj = obj[key]
    return {'present': True, 'value': obj}


def _canonical(value):
    """Lists are recursive multisets; JSON types remain distinct."""
    if isinstance(value, list):
        return ['list', sorted((_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))]
    if isinstance(value, dict):
        return ['dict', [[k, _canonical(v)] for k, v in sorted(value.items())]]
    return [type(value).__name__, value]


def _equal(a, b, comparison):
    if comparison == 'unordered':
        return _canonical(a) == _canonical(b)
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _index(records, reference=False):
    result = defaultdict(list)
    for record in records:
        docket = record['identity']['docket'] if reference else record.get('docket')
        result[_identity(docket)].append(record)
    return result


def _record_matches(case, ref):
    actual = _index(case.get('records', []) if case else [])
    expected = _index(ref['records'], reference=True)
    matches, diagnostics = {}, {'missing': [], 'additional': [], 'ambiguous': [], 'null_singleton_matches': []}
    for identity in set(actual) | set(expected):
        aa, rr = actual[identity], expected[identity]
        if len(aa) > 1 or len(rr) > 1:
            diagnostics['ambiguous'].append({'docket': identity, 'actual_count': len(aa), 'reference_count': len(rr)})
        elif aa and rr:
            matches[rr[0]['record_id']] = aa[0]
            if identity is None:
                diagnostics['null_singleton_matches'].append(rr[0]['record_id'])
        elif rr:
            diagnostics['missing'].append(rr[0]['record_id'])
        elif aa:
            diagnostics['additional'].append({'docket': aa[0].get('docket'), 'scored': ref['records_complete']})
    diagnostics['ambiguous'].sort(key=lambda x: str(x['docket']))
    diagnostics['missing'].sort()
    diagnostics['additional'].sort(key=lambda x: str(x['docket']))
    return matches, diagnostics


def _summary(fields):
    scored = [f for f in fields if f['scored']]
    eligible = sum(f['reference_state'] == 'adjudicated' and f['comparison'] in ('exact', 'unordered') and f['case_adjudicated'] for f in fields)
    n = len(scored)
    bc = sum(f['baseline']['correct'] for f in scored)
    cc = sum(f['candidate']['correct'] for f in scored)
    return {'comparable_fields': n, 'baseline_correct': bc, 'candidate_correct': cc,
            'baseline_accuracy': bc / n if n else None, 'candidate_accuracy': cc / n if n else None,
            'eligible_labels': eligible, 'scored_labels': n, 'label_coverage': n / eligible if eligible else None,
            'classifications': dict(Counter(f['classification'] for f in fields))}


def evaluate(baseline, candidate, reference=None):
    """Return transparent paired accuracy and unscored change diagnostics.

    Case paths address canonical case objects; record paths address finalized
    records. Execution errors and missing records are reported separately.
    """
    runs = {'baseline': _cases(baseline), 'candidate': _cases(candidate)}
    if reference is not None:
        from appeal_reference import validate_reference
        validate_reference(reference)
    else:
        reference = {'reference_id': None, 'version': None, 'cases': []}
    reference = {**reference, 'cases': list(reference['cases'])}
    referenced = {c['case_id'] for c in reference['cases']}
    for case_id in sorted((set(runs['baseline']) | set(runs['candidate'])) - referenced):
        b, c = runs['baseline'].get(case_id, {}), runs['candidate'].get(case_id, {})
        identities = _index(b.get('records', []) + c.get('records', []))
        records = []
        for i, (identity, entries) in enumerate(sorted(identities.items(), key=lambda x: str(x[0]))):
            keys = sorted(set().union(*(r.keys() for r in entries)))
            records.append({'record_id': 'unlabelled-' + str(i), 'identity': {'docket': identity},
                            'fields': [_unknown([key]) for key in keys]})
        reference['cases'].append({'case_id': case_id, 'source_sha256': b.get('source', {}).get('sha256') or c.get('source', {}).get('sha256'),
                                   'adjudication': {'state': 'unknown'}, 'records_complete': False,
                                   'fields': [_unknown(['scope']), _unknown(['scope_category'])], 'records': records})
    output = []
    for ref in reference['cases']:
        cases = {side: run.get(ref['case_id']) for side, run in runs.items()}
        reasons = []
        for side, case in cases.items():
            if case is None:
                reasons.append(side + ':missing_case')
            elif not case.get('source', {}).get('sha256'):
                reasons.append(side + ':missing_source_hash')
            elif case['source']['sha256'].lower() != ref['source_sha256'].lower():
                reasons.append(side + ':source_hash_mismatch')
        compatible = not reasons
        matches, diagnostics = {}, {}
        for side, case in cases.items():
            matches[side], diagnostics[side] = _record_matches(case, ref)
            for record in diagnostics[side]['additional']:
                record['scored'] = record['scored'] and compatible and ref['adjudication']['state'] == 'adjudicated'
        fields = []
        groups = [('case', None, ref.get('fields', []), cases)]
        groups.extend(('record', r['record_id'], r['fields'], {s: matches[s].get(r['record_id']) for s in runs}) for r in ref['records'])
        # Preserve observed facts outside a partial set of labels.
        expanded = []
        for scope, record_id, labels, objects in groups:
            paths = {tuple(label['path']) for label in labels}
            keys = {'scope', 'scope_category'} if scope == 'case' else set().union(*(obj.keys() for obj in objects.values() if obj is not None))
            expanded.append((scope, record_id, labels + [_unknown([key]) for key in sorted(keys) if (key,) not in paths], objects))
        groups = expanded
        expected_ids = {_identity(r['identity']['docket']) for r in ref['records']}
        observed = {s: _index(cases[s].get('records', []) if cases[s] else []) for s in runs}
        for identity in sorted((set(observed['baseline']) | set(observed['candidate'])) - expected_ids, key=str):
            objects = {s: observed[s][identity][0] if len(observed[s][identity]) == 1 else None for s in runs}
            keys = set().union(*(obj.keys() for obj in objects.values() if obj is not None))
            groups.append(('record', 'unlabelled:' + str(identity), [_unknown([key]) for key in sorted(keys)], objects))
        for scope, record_id, labels, objects in groups:
            for label in labels:
                values = {s: _lookup(objects[s], label['path']) for s in runs}
                reason = None
                if not compatible:
                    reason = 'incompatible_sources'
                elif ref['adjudication']['state'] != 'adjudicated':
                    reason = 'case_' + ref['adjudication']['state']
                elif label['state'] != 'adjudicated':
                    reason = 'label_' + label['state']
                elif label['comparison'] == 'prose':
                    reason = 'prose_requires_semantic_adjudication'
                elif scope == 'record' and any(record_id in diagnostics[s]['null_singleton_matches'] for s in runs):
                    reason = 'anonymous_record_identity_unconfirmed'
                elif any(objects[s] is None for s in runs):
                    reason = 'record_missing_or_ambiguous'
                scored = reason is None
                for side in runs:
                    values[side]['correct'] = (values[side]['present'] and _equal(values[side]['value'], label['value'], label['comparison'])) if scored else None
                if scored:
                    b, c = values['baseline']['correct'], values['candidate']['correct']
                    classification = 'unchanged_correct' if b and c else 'corrected' if c else 'regressed' if b else 'unchanged_error'
                else:
                    unchanged = values['baseline']['present'] == values['candidate']['present'] and _equal(values['baseline']['value'], values['candidate']['value'], label['comparison'])
                    classification = 'unchanged_unscored' if unchanged else 'changed_unscored'
                fields.append({'scope': scope, 'record_id': record_id, 'path': label['path'],
                               'reference_state': label['state'], 'reference': _lookup(label, ['value']),
                               'comparison': label['comparison'], 'case_adjudicated': ref['adjudication']['state'] == 'adjudicated',
                               'evidence': label.get('evidence', []), 'rationale': label.get('rationale'), 'label_version': label.get('version'),
                               **values, 'scored': scored, 'classification': classification, 'unscored_reason': reason})
        output.append({'case_id': ref['case_id'], 'execution': {s: c.get('execution') if c else None for s, c in cases.items()},
                       'records_comparison_basis': 'reference' if ref['case_id'] in referenced else 'observed_union_unscored',
                       'records_complete': ref['records_complete'],
                       'source_hashes': {**{s: c.get('source', {}).get('sha256') if c else None for s, c in cases.items()}, 'reference': ref['source_sha256']},
                       'compatibility': {'compatible': compatible, 'reasons': reasons},
                       'fields': fields, 'records': diagnostics, 'summary': _summary(fields)})
    return {'schema_version': 'appeal-evaluation-1',
            'reference': {k: reference[k] for k in ('reference_id', 'version')},
            'summary': _summary([f for c in output for f in c['fields']]), 'cases': output}


def _unknown(path):
    return {'path': path, 'state': 'unknown', 'comparison': 'unordered'}
