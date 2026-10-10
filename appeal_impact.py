"""Measure an experiment's data-quality impact on research facts.

Records are compared as named research facts (scope, duration, discretion and
so on) rather than raw JSON, so changed quotations or wording do not count as
changed answers. Each fact change is directional where that is observable:
an unknown becoming an answer is *more specific*, the reverse *less specific*,
and two different answers need judgment. Reference labels, when present,
verify correctness; without them every direction is an unverified signal, and
a more specific answer can still be wrong.

The fact catalog comes from the output contract (``appeal_schema``). A fact a
result's schema version does not include is *newly measured* when the other
side has it, rather than more specific. Other top-level scalar fields become
generic facts, so different extraction strategies remain comparable.
"""
from collections import Counter
import json

from appeal_schema import CASE_FACTS, QUESTIONS, RECORD_FACTS, UNKNOWN, fact, measures  # noqa: F401  (re-exported)

UNKNOWN_VALUES = (None, '', 'unclear', UNKNOWN)
CHANGES = ('less_specific', 'different', 'more_specific', 'newly_measured', 'unchanged')
# Top-level record keys the catalog interprets, and keys that only hold wording.
CATALOGED = {'docket', 'duration', 'municipal_decision', 'observed_status', 'discretion', 'appellants', 'project',
             'restrictions', 'housing_changes', 'related_dockets', 'timeline'}
WORDING = {'prose', 'description', 'descriptions', 'note', 'notes', 'evidence', 'rationale'}
# Reference label paths that address a cataloged fact.
REFERENCE_PATHS = {('duration',): 'duration', ('duration', 'days'): 'duration', ('duration', 'kind'): 'duration',
                   ('municipal_decision', 'outcome'): 'decision_outcome', ('municipal_decision', 'date'): 'decision_date',
                   ('observed_status', 'litigation'): 'litigation', ('observed_status', 'permission'): 'permission',
                   ('discretion', 'involved'): 'discretion_involved', ('discretion', 'central'): 'discretion_central',
                   ('project', 'municipality'): 'municipality', ('project', 'proposed_units'): 'units',
                   ('project', 'proposed_lots'): 'lots', ('disposition', 'municipal_appeal'): 'municipal_appeal',
                   ('disposition', 'housing'): 'housing', ('disposition', 'value'): 'disposition',
                   ('disposition',): 'disposition'}


def _get(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _scalar(value):
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return ', '.join(sorted(str(v) for v in value)) if value else None
    return None if isinstance(value, (dict, list)) else value


def generic_facts(records):
    """Facts for uncataloged top-level scalar fields, e.g. from another producer."""
    cataloged = CATALOGED | {spec['key'] for spec in RECORD_FACTS}
    keys = sorted({k for r in records for k in r if k not in cataloged and k not in WORDING
                   and not isinstance(r[k], dict)})
    return [fact('field:'+key, 'Other fields', key.replace('_', ' ').capitalize(),
                 lambda r, key=key: _scalar(r.get(key)), lambda r: []) for key in keys]


def known(value):
    return value not in UNKNOWN_VALUES


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
    return ' '.join(docket.strip().casefold().split()) if isinstance(docket, str) and docket != UNKNOWN else None


def processing(status):
    if status in (None, 'not_run'):
        return 'absent'
    if status == 'complete':
        return 'complete'
    return 'running' if status == 'in_progress' else 'failed'


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
    status = {'baseline': _get(b, 'status', 'state'), 'candidate': _get(c, 'status', 'state')}
    states = {side: processing(value) for side, value in status.items()}
    flow = ('recovered' if states['baseline'] == 'failed' and states['candidate'] == 'complete'
            else 'broke' if states['baseline'] == 'complete' and states['candidate'] == 'failed'
            else 'still_failing' if states['baseline'] == states['candidate'] == 'failed'
            else 'in_progress' if 'running' in states.values()
            else 'missing' if 'absent' in states.values() else 'ok')
    scope = {'baseline': _get(b, 'disposition', 'value'), 'candidate': _get(c, 'disposition', 'value')}
    scope_changed = bool(baseline and candidate and scope['baseline'] != scope['candidate'])
    verified = _verified(evaluation_case, reference_case)
    records = {side: {} for side in ('baseline', 'candidate')}
    for side, run in (('baseline', b), ('candidate', c)):
        for record in run.get('records', []) or []:
            records[side].setdefault(identity(record.get('docket')), record)
    specs = RECORD_FACTS + generic_facts([*records['baseline'].values(), *records['candidate'].values()])
    facts = []

    schemas = {'baseline': b.get('schema'), 'candidate': c.get('schema')}

    def add(record_key, docket, spec, b_obj, c_obj):
        before = spec['value'](b_obj) if b_obj is not None else None
        after = spec['value'](c_obj) if c_obj is not None else None
        scored = verified.get((record_key, spec['key']))
        newly = spec.get('since') and not measures(spec, schemas['baseline']) and measures(spec, schemas['candidate'])
        facts.append({'key': spec['key'], 'question': spec['question'], 'label': spec['label'], 'record': docket,
                      'baseline': display(before), 'candidate': display(after),
                      'change': 'newly_measured' if newly else transition(spec, before, after, b_obj, c_obj),
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
    unverified = [f for f in facts if not f['verified'] and f['key'] != 'disposition' and f['change'] != 'newly_measured']
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
        judgment.append(f'Disposition {_plain(scope["baseline"])} → {_plain(scope["candidate"])}')
    # Records follow the disposition; a disposition change needs judgment before counting them.
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
            'processed': sum(_get(c, 'status', 'state') == 'complete' for c in cases),
            'failed': sum(processing(_get(c, 'status', 'state')) == 'failed' for c in cases),
            'uncertain': sum(_get(c, 'disposition', 'value') == 'uncertain' for c in cases),
            'records': len(records),
            'duration_known': sum(_get(r, 'duration', 'kind') in ('exact', 'at_least') for r in records),
            'duration_exact': sum(_get(r, 'duration', 'kind') == 'exact' for r in records),
            'facts_known': sum(known(v) for v in slots), 'fact_slots': len(slots),
            'warnings': sum(len(_get(c, 'status', 'warnings') or []) for c in cases),
            'unverified_quotes': sum(len(_get(c, 'status', 'quotes', 'unverified') or []) for c in cases)}


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
            'statuses': dict(Counter(_get(c, 'status', 'state') or 'unknown' for c in cases)),
            'dispositions': dict(Counter(_get(c, 'disposition', 'value') or 'none' for c in cases))}


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
    """Compare two loaded runs (``pipeline.runs.load``) over their case identities, grouped by role."""
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
