"""Compare two results maps fact by fact, optionally against reference answers.

Records are compared as named research facts (scope, duration, discretion and
so on) rather than raw JSON, so changed quotations or wording do not count as
changed answers. Each fact change is directional where that is observable:
an unknown becoming an answer is *more specific*, the reverse *less specific*,
and two different answers need judgment. Reference labels, when present,
verify correctness; without them every direction is an unverified signal, and
a more specific answer can still be wrong.

The fact catalog comes from the results contract. A fact a
result's schema version does not include is *newly measured* when the other
side has it, rather than more specific. Other top-level scalar fields become
generic facts, so different extraction strategies remain comparable.
"""
from collections import Counter
import json

from court.contracts.results import CASE_FACTS, QUESTIONS, RECORD_FACTS, UNKNOWN, fact, measures
from court.core.datasets import is_failure

UNKNOWN_VALUES = (None, '', 'unclear', UNKNOWN)
CHANGES = ('less_specific', 'different', 'more_specific', 'newly_measured', 'unchanged')
# Top-level record keys the catalog interprets, and keys that only hold wording.
CATALOGED = {'docket', 'duration', 'municipal_decision', 'observed_status', 'discretion', 'appellants', 'project',
             'restrictions', 'housing_changes', 'related_dockets', 'timeline'}
WORDING = {'prose', 'description', 'descriptions', 'note', 'notes', 'evidence', 'rationale'}


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


def state(value, settled=False):
    """complete, failed, not_run (a call this run did not send), or None when a map has no item.

    In a settled map, such as a finished production run, an unsent call is final: the item failed.
    """
    if value is None:
        return None
    if is_failure(value):
        return 'not_run' if value.kind == 'not_sent' and not settled else 'failed'
    return 'complete'


def processing(status):
    return {None: 'absent', 'not_run': 'absent', 'complete': 'complete'}.get(status, 'failed')


def scored_facts(reference, baseline, candidate):
    """Reference classifications keyed by (docket identity or None, fact key).

    A reference item is ``{"result": a result, "labeled": {"case": [...], "records": {docket: [...]}}}``.
    ``"all"`` labels every fact of the case, of every record, or of one record. A fact not listed is
    not labeled and is not scored, so an item without ``labeled`` scores nothing.
    """
    if not reference:
        return {}
    truth, labeled = reference['result'], reference.get('labeled') or {}

    def chosen(listing, key):
        return listing == 'all' or key in (listing or [])
    found = {}

    def score(record_key, spec, ref_obj, b_obj, c_obj):
        expected = spec['value'](ref_obj) if ref_obj is not None else None
        normalize = spec['normalize'] or (lambda v: v)

        def right(obj):
            value = spec['value'](obj) if obj is not None else None
            return value == expected or known(value) and known(expected) and normalize(value) == normalize(expected)
        before, after = right(b_obj), right(c_obj)
        found[(record_key, spec['key'])] = ('corrected' if after and not before else 'regressed' if before and not after
                                            else 'unchanged_correct' if after else 'unchanged_error')
    for spec in CASE_FACTS:
        if chosen(labeled.get('case'), spec['key']):
            score(None, spec, truth, baseline or None, candidate or None)
    sides = {name: {identity(r.get('docket')): r for r in (obj or {}).get('records') or []}
             for name, obj in (('baseline', baseline), ('candidate', candidate))}
    records = labeled.get('records') or {}
    for record in truth.get('records') or []:
        key = identity(record.get('docket'))
        listing = records if records == 'all' else records.get(record.get('docket'))
        for spec in RECORD_FACTS:
            if chosen(listing, spec['key']):
                score(key, spec, record, sides['baseline'].get(key), sides['candidate'].get(key))
    return found


def _plain(value):
    return str(value or 'unknown').replace('_', ' ')


def _plural(n, word):
    return f'{n} {word}{"s" * (n != 1)}'


INPUT_NOTES = {'removed': 'Not in the candidate\'s input: merged into another case or removed before this step',
               'added': 'New in the candidate\'s input', 'changed': 'Input changed before this step'}


def compare_case(case_id, baseline, candidate, reference=None, settled=False, input_change=None):
    """Fact-level comparison of one case's two results, with a verdict.

    ``baseline`` and ``candidate`` are results, failures or None (absent); ``settled`` marks the
    baseline as a finished run (see ``state``). ``input_change`` is how the case's input to the
    compared step differs (changed, added, removed), ``same`` when the change being tested
    reached other cases' inputs but not this one's, or None when no input changed.
    """
    status = {'baseline': state(baseline, settled), 'candidate': state(candidate)}
    failures = {side: value.to_json() for side, value in (('baseline', baseline), ('candidate', candidate))
                if is_failure(value)}
    baseline = None if is_failure(baseline) else baseline
    candidate = None if is_failure(candidate) else candidate
    if 'not_run' in status.values():
        # A side that did not run says nothing about the case's answers.
        baseline = candidate = None
    b, c = baseline or {}, candidate or {}
    states = {side: processing(value) for side, value in status.items()}
    flow = ('recovered' if states['baseline'] == 'failed' and states['candidate'] == 'complete'
            else 'broke' if states['baseline'] == 'complete' and states['candidate'] == 'failed'
            else 'still_failing' if states['baseline'] == states['candidate'] == 'failed'
            else 'missing' if 'absent' in states.values() else 'ok')
    scope = {'baseline': _get(b, 'disposition', 'value'), 'candidate': _get(c, 'disposition', 'value')}
    scope_changed = bool(baseline is not None and candidate is not None and scope['baseline'] != scope['candidate'])
    verified = scored_facts(reference, b, c)
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

    if baseline is not None and candidate is not None:
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
        better.append(f'Recovered from a {_plain(failures["baseline"]["kind"])} failure')
    if flow == 'broke':
        worse.append(f'Broke: {_plain(failures["candidate"]["kind"])} failure')
    if flow == 'still_failing':
        notes.append(f'Still failing: {_plain(failures["candidate"]["kind"])}')
    if flow == 'missing':
        unsent = [side for side in ('baseline', 'candidate') if status[side] == 'not_run']
        notes.append(f'Not run in the {unsent[0]}: it needs calls that run did not send' if unsent
                     else 'Only in '+('candidate' if status['baseline'] is None else 'baseline'))
    if scope_changed:
        judgment.append(f'Disposition {_plain(scope["baseline"])} → {_plain(scope["candidate"])}')
    # Records follow the disposition, and move with documents when cases merge or split: either needs
    # judgment before counting them.
    moved = scope_changed or input_change in ('added', 'removed')
    if gained:
        (judgment if moved else better).append('+'+_plural(len(gained), 'record'))
    if lost:
        (judgment if moved else worse).append('−'+_plural(len(lost), 'record'))
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
    if input_change in INPUT_NOTES:
        notes.append(INPUT_NOTES[input_change])
    elif input_change == 'same' and verdict != 'unchanged':
        notes.append('Same input as the baseline: changed by chance or by how cases were batched')
    chips = [*worse, *better, *judgment, *notes]
    if wording_only and not chips:
        chips.append('Wording or citations only')
    rank = {'regressed': 0, 'corrected': 1}
    highlights = sorted((f for f in facts if f['change'] != 'unchanged' or f['verified'] in rank),
                        key=lambda f: (rank.get(f['verified'], 2), CHANGES.index(f['change'])))
    return {'case_id': case_id, 'verdict': verdict, 'failures': failures, 'input': input_change,
            'verified': bool(scored['corrected'] or scored['regressed']),
            'processing': {'flow': flow, **status},
            'records': {'baseline': len(records['baseline']), 'candidate': len(records['candidate']),
                        'gained': gained, 'lost': lost, 'matched': len(matched), 'wording_only': wording_only},
            'chips': chips,
            'signals': {'better': better, 'worse': worse, 'judgment': judgment, 'notes': notes},
            'highlights': [{k: f[k] for k in ('label', 'record', 'baseline', 'candidate', 'change', 'verified')}
                           for f in highlights[:3]],
            'facts': facts, 'one_sided': one_sided}


def record_metrics(values):
    """Data-quality measures for one side's results (results or failures)."""
    results = [v for v in values if v is not None and not is_failure(v)]
    records = [r for c in results for r in c.get('records', []) or []]
    slots = [spec['value'](r) for r in records for spec in RECORD_FACTS]
    return {'cases': len(values), 'processed': len(results),
            'failed': sum(is_failure(v) for v in values if v is not None),
            'uncertain': sum(_get(c, 'disposition', 'value') == 'uncertain' for c in results),
            'records': len(records),
            'duration_known': sum(_get(r, 'duration', 'kind') in ('exact', 'at_least') for r in records),
            'duration_exact': sum(_get(r, 'duration', 'kind') == 'exact' for r in records),
            'facts_known': sum(known(v) for v in slots), 'fact_slots': len(slots)}


def profile(values):
    """Per-fact completeness for one results map, to show where answers are missing."""
    values = list(values)
    results = [v for v in values if not is_failure(v)]
    records = [r for c in results for r in c.get('records', []) or []]
    rows = []
    for spec in RECORD_FACTS:
        answers = [spec['value'](r) for r in records]
        rows.append({'key': spec['key'], 'question': spec['question'], 'label': spec['label'],
                     'known': sum(known(v) for v in answers), 'total': len(answers),
                     'top': Counter(str(display(v)) for v in answers if known(v)).most_common(3)})
    return {'metrics': record_metrics(values), 'facts': rows,
            'statuses': dict(Counter(state(v) for v in values)),
            'failures': dict(Counter(v.kind for v in values if is_failure(v))),
            'dispositions': dict(Counter(_get(c, 'disposition', 'value') or 'none' for c in results))}


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


def compare(baseline, candidate, reference=None, keys=None, settled=False, inputs=None):
    """Compare two results maps over their shared or chosen case IDs.

    ``reference`` is an optional map of reference items (see ``scored_facts``); ``settled`` marks
    the baseline as a finished run (see ``state``). ``inputs`` maps case IDs whose input to the
    compared step differs to how (see ``compare_case``); when it has any, every other case is
    ``same``.
    """
    reference, inputs = reference or {}, inputs or {}
    ids = sorted(keys if keys is not None else set(baseline) | set(candidate))
    cases = [compare_case(i, baseline.get(i), candidate.get(i), reference.get(i), settled,
                          inputs.get(i, 'same') if inputs else None) for i in ids]
    facts = [f for c in cases for f in c['facts']]
    compared = [f for f in facts if f['baseline'] is not None or f['candidate'] is not None]
    fact_map = []
    for key in dict.fromkeys(f['key'] for f in facts):
        rows = [f for f in facts if f['key'] == key]
        # Reference-scored facts count once, as verified, rather than as unverified changes.
        open_rows = [f for f in rows if f['verified'] not in ('corrected', 'regressed')]
        fact_map.append({'key': key, 'question': rows[0]['question'], 'label': rows[0]['label'], 'total': len(rows),
                         **{change: sum(f['change'] == change for f in open_rows) for change in CHANGES},
                         'corrected': sum(f['verified'] == 'corrected' for f in rows),
                         'regressed': sum(f['verified'] == 'regressed' for f in rows)})
    scored = [f for f in facts if f['verified']]
    summary = {
        'cases': len(cases), 'verdicts': dict(Counter(c['verdict'] for c in cases)),
        'flows': dict(Counter(c['processing']['flow'] for c in cases)),
        'inputs': dict(Counter(c['input'] for c in cases if c['input'])),
        'baseline': record_metrics([baseline.get(i) for i in ids]),
        'candidate': record_metrics([candidate.get(i) for i in ids]),
        'stability': {'unchanged': sum(f['change'] == 'unchanged' for f in compared), 'compared': len(compared)},
        'records': {'gained': sum(len(c['records']['gained']) for c in cases),
                    'lost': sum(len(c['records']['lost']) for c in cases)},
        'fact_map': fact_map, 'headline': headline(cases)}
    accuracy = None
    if scored:
        accuracy = {'scored_facts': len(scored),
                    'baseline_correct': sum(f['verified'] in ('regressed', 'unchanged_correct') for f in scored),
                    'candidate_correct': sum(f['verified'] in ('corrected', 'unchanged_correct') for f in scored),
                    'corrected': sum(f['verified'] == 'corrected' for f in scored),
                    'regressed': sum(f['verified'] == 'regressed' for f in scored)}
    return {'cases': cases, 'summary': summary, 'questions': QUESTIONS, 'accuracy': accuracy}


def regressions(comparison):
    """Cases that got worse: broke, worsened, mixed, or a verified regression."""
    return [c['case_id'] for c in comparison['cases']
            if c['verdict'] in ('worsened', 'mixed') or c['processing']['flow'] == 'broke'
            or any(f['verified'] == 'regressed' for f in c['facts'])]


def unfinished(comparison):
    """Cases a side did not run because a call was not sent: the round is incomplete for them."""
    return [c['case_id'] for c in comparison['cases'] if 'not_run' in c['processing'].values()]
