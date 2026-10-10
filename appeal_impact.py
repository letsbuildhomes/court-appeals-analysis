"""Measure an experiment's data-quality impact on research facts.

Records are compared as named research facts (scope, duration, discretion and
so on) rather than raw JSON, so changed quotations or wording do not count as
changed answers. Each fact change is directional where that is observable:
an unknown becoming an answer is *more specific*, the reverse *less specific*,
and two different answers need judgment. Reference labels, when present,
verify correctness; without them every direction is an unverified signal, and
a more specific answer can still be wrong.

The catalog covers the appeals record schema. Other top-level scalar fields
become generic facts, so different extraction strategies remain comparable.
"""
from collections import Counter
import json
import re

UNKNOWN = (None, '', 'unclear', 'unknown')
SPECIFICITY = {'unknown': 0, 'at_least': 1, 'exact': 2}
CHANGES = ('less_specific', 'different', 'more_specific', 'unchanged')
# Top-level record keys the catalog interprets, and keys that only hold wording.
CATALOGED = {'docket', 'duration', 'duration_bounds', 'municipal_decision', 'observed_status', 'discretion',
             'appellants', 'project', 'restrictions', 'housing_changes', 'related_dockets', 'timeline'}
WORDING = {'prose', 'description', 'descriptions', 'note', 'notes', 'evidence', 'rationale'}


def _get(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _evidence(*values):
    quotes = []
    for value in values:
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and isinstance(item.get('quote'), str):
                quotes.append({'doc': item.get('doc'), 'page': item.get('page'), 'quote': item['quote']})
    return quotes[:6]


def _items_evidence(key):
    return lambda r: _evidence(*[i.get('evidence') for i in r.get(key) or [] if isinstance(i, dict)])


def _object_evidence(*path):
    return lambda r: _evidence(_get(r, *path))


def _duration(record):
    duration = record.get('duration')
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        return f'{duration} days'
    kind, days = _get(record, 'duration', 'kind'), _get(record, 'duration', 'days')
    if kind not in ('exact', 'at_least') or days is None:
        return None
    return f'{days} days' if kind == 'exact' else f'≥ {days} days'


def _endpoint(side):
    def value(record):
        point = _get(record, 'duration_bounds', side) or {}
        if not isinstance(point, dict) or not point.get('date'):
            return None
        relation = {'on_or_before': 'on or before ', 'on_or_after': 'on or after '}.get(point.get('relation'), '')
        return relation+point['date']
    return value


def _count(key, empty_known=True):
    def value(record):
        items = record.get(key)
        if not isinstance(items, list) or (not items and not empty_known):
            return None
        return len(items)
    return value


def _representation(record):
    values = sorted(str(a.get('representation') or 'unknown') for a in record.get('appellants') or [] if isinstance(a, dict))
    if not values or all(v in UNKNOWN for v in values):
        return None
    return ', '.join(f'{n} {v}' for v, n in sorted(Counter(values).items()))


def _related(record):
    dockets = sorted({' '.join(str(d.get('docket')).split()) for d in record.get('related_dockets') or []
                      if isinstance(d, dict) and d.get('docket')})
    return ', '.join(dockets) if dockets else None


def _municipality(value):
    """'Town of Jericho' and 'Jericho, Vermont' name the same place."""
    text = re.sub(r'^(town|city|village) of\s+', '', str(value).strip().casefold())
    return re.sub(r',?\s*(vermont|vt\.?)$', '', text).strip()


def _field(*path):
    return lambda record: _get(record, *path)


def fact(key, question, label, value, evidence, rank=None, normalize=None):
    return {'key': key, 'question': question, 'label': label, 'value': value, 'evidence': evidence,
            'rank': rank, 'normalize': normalize}


RECORD_FACTS = [
    fact('duration', 'Duration', 'Appeal duration', _duration,
         lambda r: _evidence(_get(r, 'duration_bounds', 'start', 'evidence'), _get(r, 'duration_bounds', 'end', 'evidence')),
         rank=lambda r: SPECIFICITY.get(_get(r, 'duration', 'kind'), 0)),
    fact('start', 'Duration', 'Start date', _endpoint('start'), _object_evidence('duration_bounds', 'start', 'evidence'),
         rank=lambda r: 2 if _get(r, 'duration_bounds', 'start', 'relation') == 'exact' else 1),
    fact('end', 'Duration', 'End date', _endpoint('end'), _object_evidence('duration_bounds', 'end', 'evidence'),
         rank=lambda r: 2 if _get(r, 'duration_bounds', 'end', 'relation') == 'exact' else 1),
    fact('decision_outcome', 'Decision & status', 'Municipal decision', _field('municipal_decision', 'outcome'),
         _object_evidence('municipal_decision', 'evidence')),
    fact('decision_date', 'Decision & status', 'Decision date', _field('municipal_decision', 'date'),
         _object_evidence('municipal_decision', 'evidence')),
    fact('litigation', 'Decision & status', 'Litigation status', _field('observed_status', 'litigation'),
         _object_evidence('observed_status', 'evidence')),
    fact('permission', 'Decision & status', 'Permission status', _field('observed_status', 'permission'),
         _object_evidence('observed_status', 'evidence')),
    fact('discretion_involved', 'Discretion', 'Discretion involved', _field('discretion', 'involved'),
         _object_evidence('discretion', 'evidence')),
    fact('discretion_central', 'Discretion', 'Discretion central', _field('discretion', 'central'),
         _object_evidence('discretion', 'central_evidence')),
    fact('appellants', 'Appellants', 'Appellants', _count('appellants', empty_known=False), _items_evidence('appellants')),
    fact('representation', 'Appellants', 'Representation', _representation, _items_evidence('appellants')),
    fact('municipality', 'Project', 'Municipality', _field('project', 'municipality'), _object_evidence('project', 'evidence'),
         normalize=_municipality),
    fact('units', 'Project', 'Proposed units', _field('project', 'proposed_units'), _object_evidence('project', 'evidence')),
    fact('lots', 'Project', 'Proposed lots', _field('project', 'proposed_lots'), _object_evidence('project', 'evidence')),
    fact('restrictions', 'Effects', 'Restrictions', _count('restrictions'), _items_evidence('restrictions')),
    fact('housing_changes', 'Effects', 'Housing changes', _count('housing_changes'), _items_evidence('housing_changes')),
    fact('related', 'Links', 'Related dockets', _related, _items_evidence('related_dockets')),
]
CASE_FACTS = [
    fact('scope_category', 'Scope', 'Scope category', lambda c: c.get('scope_category'), _object_evidence('scope', 'evidence')),
    fact('municipal_appeal', 'Scope', 'Municipal appeal', _field('scope', 'municipal_appeal'), _object_evidence('scope', 'evidence')),
    fact('housing', 'Scope', 'Housing', _field('scope', 'housing'), _object_evidence('scope', 'evidence')),
]
QUESTIONS = ['Scope', 'Duration', 'Decision & status', 'Discretion', 'Appellants', 'Project', 'Effects', 'Links', 'Other fields']
# Reference label paths that address a cataloged fact.
REFERENCE_PATHS = {('duration',): 'duration', ('duration', 'days'): 'duration', ('duration', 'kind'): 'duration',
                   ('municipal_decision', 'outcome'): 'decision_outcome', ('municipal_decision', 'date'): 'decision_date',
                   ('observed_status', 'litigation'): 'litigation', ('observed_status', 'permission'): 'permission',
                   ('discretion', 'involved'): 'discretion_involved', ('discretion', 'central'): 'discretion_central',
                   ('project', 'municipality'): 'municipality', ('project', 'proposed_units'): 'units',
                   ('project', 'proposed_lots'): 'lots', ('scope', 'municipal_appeal'): 'municipal_appeal',
                   ('scope', 'housing'): 'housing', ('scope_category',): 'scope_category'}


def _scalar(value):
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return ', '.join(sorted(str(v) for v in value)) if value else None
    return None if isinstance(value, (dict, list)) else value


def generic_facts(records):
    """Facts for uncataloged top-level scalar fields, e.g. from another producer."""
    keys = sorted({k for r in records for k in r if k not in CATALOGED and k not in WORDING
                   and not isinstance(r[k], dict)})
    return [fact('field:'+key, 'Other fields', key.replace('_', ' ').capitalize(),
                 lambda r, key=key: _scalar(r.get(key)), lambda r: []) for key in keys]


def known(value):
    return value not in UNKNOWN


def display(value):
    if not known(value):
        return None
    return str(value).replace('_', ' ') if isinstance(value, str) else value


def transition(spec, before, after, b_obj=None, c_obj=None):
    normalize = spec['normalize'] or (lambda v: v)
    if before == after or known(before) and known(after) and normalize(before) == normalize(after):
        return 'unchanged'
    if not known(before) and known(after):
        return 'more_specific'
    if known(before) and not known(after):
        return 'less_specific'
    if not known(before):
        return 'unchanged'
    if spec['rank']:
        b, c = spec['rank'](b_obj), spec['rank'](c_obj)
        if b != c:
            return 'more_specific' if c > b else 'less_specific'
    return 'different'


def identity(docket):
    return ' '.join(docket.strip().casefold().split()) if isinstance(docket, str) else None


def processing(status):
    if status is None:
        return 'absent'
    if status == 'complete':
        return 'complete'
    return 'running' if status in ('running', 'reviewing', 'triaging', 'extracting', 'measuring') else 'failed'


def _verified(evaluation_case, reference_case):
    """Scored reference classifications, keyed by (docket identity or None, fact key)."""
    dockets = {r['record_id']: identity(r['identity'].get('docket'))
               for r in (reference_case or {}).get('records', [])}
    result = {}
    for field in (evaluation_case or {}).get('fields', []):
        path = tuple(field['path'])
        key = REFERENCE_PATHS.get(path) or ('field:'+path[0] if len(path) == 1 else None)
        if not field.get('scored') or key is None:
            continue
        record = None if field['scope'] == 'case' else dockets.get(field.get('record_id'))
        result[(record, key)] = field['classification']
    return result


def _plain(value):
    return str(value or 'unknown').replace('_', ' ')


def _plural(n, word):
    return f'{n} {word}{"s" * (n != 1)}'


def compare_case(baseline, candidate, evaluation_case=None, reference_case=None, role='selected'):
    """Fact-level comparison of one case's two outputs, with a verdict."""
    b, c = baseline or {}, candidate or {}
    status = {'baseline': _get(b, 'execution', 'status'), 'candidate': _get(c, 'execution', 'status')}
    states = {side: processing(value) for side, value in status.items()}
    flow = ('recovered' if states['baseline'] == 'failed' and states['candidate'] == 'complete'
            else 'broke' if states['baseline'] == 'complete' and states['candidate'] == 'failed'
            else 'still_failing' if states['baseline'] == states['candidate'] == 'failed'
            else 'in_progress' if 'running' in states.values()
            else 'missing' if 'absent' in states.values() else 'ok')
    scope = {'baseline': b.get('scope_category'), 'candidate': c.get('scope_category')}
    scope_changed = bool(baseline and candidate and scope['baseline'] != scope['candidate'])
    verified = _verified(evaluation_case, reference_case)
    records = {side: {} for side in ('baseline', 'candidate')}
    for side, run in (('baseline', b), ('candidate', c)):
        for record in run.get('records', []) or []:
            records[side].setdefault(identity(record.get('docket')), record)
    specs = RECORD_FACTS + generic_facts([*records['baseline'].values(), *records['candidate'].values()])
    facts = []

    def add(record_key, docket, spec, b_obj, c_obj):
        before = spec['value'](b_obj) if b_obj is not None else None
        after = spec['value'](c_obj) if c_obj is not None else None
        scored = verified.get((record_key, spec['key']))
        facts.append({'key': spec['key'], 'question': spec['question'], 'label': spec['label'], 'record': docket,
                      'baseline': display(before), 'candidate': display(after),
                      'change': transition(spec, before, after, b_obj, c_obj),
                      'verified': scored if scored in ('corrected', 'regressed', 'unchanged_error', 'unchanged_correct') else None,
                      'evidence': {'baseline': spec['evidence'](b_obj) if b_obj is not None else [],
                                   'candidate': spec['evidence'](c_obj) if c_obj is not None else []}})

    if baseline and candidate:
        for spec in CASE_FACTS:
            add(None, None, spec, b, c)
    matched = [k for k in records['baseline'] if k in records['candidate']]
    gained = [records['candidate'][k].get('docket') for k in records['candidate'] if k not in records['baseline']]
    lost = [records['baseline'][k].get('docket') for k in records['baseline'] if k not in records['candidate']]
    wording_only = []
    for key in matched:
        before, after = records['baseline'][key], records['candidate'][key]
        start = len(facts)
        for spec in specs:
            add(key, before.get('docket'), spec, before, after)
        if all(f['change'] == 'unchanged' for f in facts[start:]) and \
                json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
            wording_only.append(before.get('docket'))
    one_sided = []
    for side, dockets in (('candidate', gained), ('baseline', lost)):
        for docket in dockets:
            record = records[side][identity(docket)]
            one_sided.append({'side': side, 'docket': docket, 'facts': [
                {'key': s['key'], 'question': s['question'], 'label': s['label'], 'value': display(s['value'](record)),
                 'evidence': s['evidence'](record)} for s in specs]})

    scored = Counter(f['verified'] for f in facts if f['verified'])
    unverified = [f for f in facts if not f['verified'] and f['key'] != 'scope_category']
    more = sum(f['change'] == 'more_specific' for f in unverified)
    less = sum(f['change'] == 'less_specific' for f in unverified)
    different = sum(f['change'] == 'different' for f in unverified)
    better, worse, judgment, notes = [], [], [], []
    if flow == 'recovered':
        better.append(f'Recovered from {_plain(status["baseline"])}')
    if flow == 'broke':
        worse.append(f'Broke: {_plain(status["candidate"])}')
    if flow == 'still_failing':
        notes.append(f'Still failing: {_plain(status["candidate"])}')
    if flow == 'in_progress':
        notes.append('In progress')
    if flow == 'missing':
        notes.append('Only in '+('candidate' if baseline is None else 'baseline'))
    if scope_changed:
        judgment.append(f'Scope {_plain(scope["baseline"])} → {_plain(scope["candidate"])}')
    # Records follow scope; a scope change needs judgment before counting them.
    if gained:
        (judgment if scope_changed else better).append('+'+_plural(len(gained), 'record'))
    if lost:
        (judgment if scope_changed else worse).append('−'+_plural(len(lost), 'record'))
    if scored['corrected']:
        better.append(_plural(scored['corrected'], 'verified correction'))
    if scored['regressed']:
        worse.append(_plural(scored['regressed'], 'verified regression'))
    if more:
        better.append(f'{more} more specific')
    if less:
        worse.append(f'{less} less specific')
    if different:
        judgment.append(_plural(different, 'different answer'))
    verdict = ('mixed' if better and worse else 'worsened' if worse else 'improved' if better
               else 'changed' if judgment else 'unchanged')
    chips = [*worse, *better, *judgment, *notes]
    if wording_only and not chips:
        chips.append('Wording or citations only')
    rank = {'regressed': 0, 'corrected': 1}
    highlights = sorted((f for f in facts if f['change'] != 'unchanged' or f['verified'] in rank),
                        key=lambda f: (rank.get(f['verified'], 2), CHANGES.index(f['change'])))
    return {'case_id': (b or c).get('case_id'), 'role': role, 'verdict': verdict,
            'verified': bool(scored['corrected'] or scored['regressed']),
            'processing': {'flow': flow, **status},
            'records': {'baseline': len(records['baseline']), 'candidate': len(records['candidate']),
                        'gained': gained, 'lost': lost, 'matched': len(matched), 'wording_only': wording_only},
            'chips': chips,
            'signals': {'better': better, 'worse': worse, 'judgment': judgment, 'notes': notes},
            'highlights': [{k: f[k] for k in ('label', 'record', 'baseline', 'candidate', 'change', 'verified')}
                           for f in highlights[:3]],
            'facts': facts, 'one_sided': one_sided}


def record_metrics(cases):
    """Data-quality measures for one side's cases."""
    records = [r for c in cases for r in c.get('records', []) or []]
    slots = [spec['value'](r) for r in records for spec in RECORD_FACTS]
    return {'cases': len(cases),
            'processed': sum(_get(c, 'execution', 'status') == 'complete' for c in cases),
            'failed': sum(processing(_get(c, 'execution', 'status')) == 'failed' for c in cases),
            'records': len(records),
            'duration_known': sum(_duration(r) is not None for r in records),
            'duration_exact': sum(_get(r, 'duration', 'kind') == 'exact' for r in records),
            'facts_known': sum(known(v) for v in slots), 'fact_slots': len(slots),
            'warnings': sum(len(_get(c, 'execution', 'warnings') or []) for c in cases)}


def profile(cases):
    """Per-fact completeness for a single run, to show where answers are missing."""
    records = [r for c in cases for r in c.get('records', []) or []]
    rows = []
    for spec in RECORD_FACTS:
        values = [spec['value'](r) for r in records]
        rows.append({'key': spec['key'], 'question': spec['question'], 'label': spec['label'],
                     'known': sum(known(v) for v in values), 'total': len(values),
                     'top': Counter(str(display(v)) for v in values if known(v)).most_common(3)})
    return {'metrics': record_metrics(cases), 'facts': rows,
            'statuses': dict(Counter(_get(c, 'execution', 'status') or 'unknown' for c in cases)),
            'scopes': dict(Counter(c.get('scope_category') or 'unknown' for c in cases))}


def headline(cases):
    """One sentence describing the dominant effect on these cases."""
    if not cases:
        return 'No cases were compared.'
    verdicts = Counter(c['verdict'] for c in cases)
    flows = Counter(c['processing']['flow'] for c in cases)
    # Recovery and breakage are named first; the remaining verdicts follow.
    for flow, verdict in (('recovered', 'improved'), ('broke', 'worsened')):
        verdicts[verdict] -= sum(c['processing']['flow'] == flow and c['verdict'] == verdict for c in cases)
    parts = [f'{flows["recovered"]} of {len(cases)} recovered from failed processing'] if flows['recovered'] else []
    if flows['broke']:
        parts.append(f'{flows["broke"]} broke')
    labels = {'improved': 'otherwise improved' if flows['recovered'] else 'improved', 'worsened': 'worsened',
              'mixed': 'mixed', 'changed': 'need judgment', 'unchanged': 'unchanged'}
    parts += [f'{verdicts[key]} {text}' for key, text in labels.items() if verdicts[key] > 0]
    text = '; '.join(parts)+'.'
    return text[0].upper()+text[1:]


def impact(baseline, candidate, evaluation=None, reference=None, roles=None):
    """Compare two canonical runs over their case identities, grouped by role."""
    roles = roles or {}
    sides = {side: {c['case_id']: c for c in run['cases']} for side, run in (('baseline', baseline), ('candidate', candidate))}
    evaluated = {c['case_id']: c for c in (evaluation or {}).get('cases', [])}
    references = {c['case_id']: c for c in (reference or {}).get('cases', [])}
    ids = sorted(set(sides['baseline']) | set(sides['candidate']))
    cases = [compare_case(sides['baseline'].get(i), sides['candidate'].get(i), evaluated.get(i), references.get(i),
                          roles.get(i, 'selected')) for i in ids]
    groups = {}
    for role in ('selected', 'companion'):
        chosen = [c for c in cases if c['role'] == role]
        if not chosen:
            continue
        facts = [f for c in chosen for f in c['facts']]
        compared = [f for f in facts if f['baseline'] is not None or f['candidate'] is not None]
        keys = list(dict.fromkeys(f['key'] for f in facts))
        fact_map = []
        for key in keys:
            rows = [f for f in facts if f['key'] == key]
            # Reference-scored facts count once, as verified, rather than as unverified changes.
            open_rows = [f for f in rows if f['verified'] not in ('corrected', 'regressed')]
            fact_map.append({'key': key, 'question': rows[0]['question'], 'label': rows[0]['label'], 'total': len(rows),
                             **{change: sum(f['change'] == change for f in open_rows) for change in CHANGES},
                             'corrected': sum(f['verified'] == 'corrected' for f in rows),
                             'regressed': sum(f['verified'] == 'regressed' for f in rows)})
        groups[role] = {
            'cases': len(chosen), 'verdicts': dict(Counter(c['verdict'] for c in chosen)),
            'flows': dict(Counter(c['processing']['flow'] for c in chosen)),
            'baseline': record_metrics([sides['baseline'][c['case_id']] for c in chosen if c['case_id'] in sides['baseline']]),
            'candidate': record_metrics([sides['candidate'][c['case_id']] for c in chosen if c['case_id'] in sides['candidate']]),
            'stability': {'unchanged': sum(f['change'] == 'unchanged' for f in compared), 'compared': len(compared)},
            'records': {'gained': sum(len(c['records']['gained']) for c in chosen),
                        'lost': sum(len(c['records']['lost']) for c in chosen)},
            'fact_map': fact_map, 'headline': headline(chosen)}
    summary = (evaluation or {}).get('summary') or {}
    return {'cases': cases, 'groups': groups, 'questions': QUESTIONS,
            'reference': bool(summary.get('comparable_fields')),
            'accuracy': {k: summary.get(k) for k in ('comparable_fields', 'baseline_correct', 'candidate_correct',
                                                      'eligible_labels', 'scored_labels')} if summary else None}
