"""Turn the September pipeline's v5 triage and v6 records into the results contract.

The legacy prompts ask for ``unclear`` and ``null``; the contract has one
``unknown``. Citations keep only ``doc``, ``page`` and ``quote``; malformed ones
are dropped with a warning rather than failing the case.
"""
from court.contracts import results as schema

U = schema.UNKNOWN


def exact_members(value, key, expected):
    if len(set(expected)) != len(expected) or not isinstance(value, list) or len(value) != len(expected) \
            or {x.get(key) for x in value} != set(expected):
        raise ValueError(f'Response must cover every {key} exactly once')


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else U


def enum(value, allowed, where, warnings):
    if value in (None, '', 'unclear', U):
        return U
    if value not in allowed:
        warnings.append(f'{where}: {value!r} is not an allowed value; recorded as unknown')
        return U
    return value


def date(value, where, warnings):
    if value in (None, '', U):
        return U
    if not isinstance(value, str) or not schema.DATE.match(value):
        warnings.append(f'{where}: invalid date {value!r}; recorded as unknown')
        return U
    return value


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else U


def evidence(value, where, warnings):
    kept = []
    for i, cite in enumerate(value if isinstance(value, list) else []):
        if isinstance(cite, dict) and type(cite.get('doc')) is int and type(cite.get('page')) is int \
                and cite['doc'] >= 1 and cite['page'] >= 1 and isinstance(cite.get('quote'), str) and cite['quote'].strip():
            kept.append({'doc': cite['doc'], 'page': cite['page'], 'quote': cite['quote']})
        else:
            warnings.append(f'{where}[{i}]: malformed citation dropped')
    return kept


def scope_disposition(triage, route):
    """The disposition a validated triage decision establishes."""
    warnings = []
    municipal = enum(triage.get('municipal_appeal'), schema.SCOPE, 'triage.municipal_appeal', warnings)
    housing = enum(triage.get('housing'), schema.SCOPE, 'triage.housing', warnings)
    if route.get('route') in ('out_of_scope', 'no_members') or 'no' in (municipal, housing):
        value = 'excluded'
    else:
        value = 'in_scope' if municipal == housing == 'yes' else 'uncertain'
    reason = text(triage.get('reason'))
    if reason == U and value == 'excluded':
        reason = ('No supplied document belongs to the matter' if route.get('route') == 'no_members'
                  else 'Triage found the matter is not a municipal housing appeal')
    return {'value': value, 'reason': reason, 'municipal_appeal': municipal, 'housing': housing,
            'evidence': evidence(triage.get('evidence'), 'triage.evidence', warnings)}


def flagged_disposition(disposition, review):
    """Review flagged the scope decision: the case stays uncertain for human review."""
    scope, warnings = review.get('scope') or {}, []
    issues = '; '.join(text(f.get('issue')) for f in review.get('flags') or [] if isinstance(f, dict))
    reason = text(scope.get('reason'))
    cited = evidence(scope.get('evidence'), 'review.scope.evidence', warnings)
    return {**disposition, 'value': 'uncertain',
            'reason': 'Review flagged the scope decision: '+(reason if reason != U else issues or 'no reason given'),
            'evidence': cited or disposition['evidence']}


def events(timeline, warnings):
    kept = []
    for i, event in enumerate(timeline or []):
        if isinstance(event, dict) and event.get('kind') in schema.ENUMS['event']:
            kept.append(event)
        else:
            warnings.append(f'timeline[{i}]: event without an allowed kind dropped')
    return kept


def endpoint(point, side, warnings):
    if not isinstance(point, dict) or not point.get('date'):
        return U
    return {'date': point['date'], 'relation': point['relation'], 'description': text(point.get('description')),
            'evidence': evidence(point.get('evidence'), f'duration.{side}.evidence', warnings)}


def record(draft, bound=None):
    """One contract record from a v6 docket record and its duration bound, with warnings."""
    w = []
    project, decision = draft.get('project') or {}, draft.get('municipal_decision') or {}
    discretion, status = draft.get('discretion') or {}, draft.get('observed_status') or {}
    start = endpoint((bound or {}).get('start'), 'start', w)
    end = endpoint((bound or {}).get('end'), 'end', w)
    kind, days = schema.measure(start, end)
    return {
        'docket': text(draft.get('docket')),
        'project': {'description': text(project.get('description')), 'municipality': text(project.get('municipality')),
                    'proposed_units': count(project.get('proposed_units')),
                    'proposed_lots': count(project.get('proposed_lots')),
                    'evidence': evidence(project.get('evidence'), 'project.evidence', w)},
        'municipal_decision': {'description': text(decision.get('description')),
                               'date': date(decision.get('date'), 'municipal_decision.date', w),
                               'outcome': enum(decision.get('outcome'), schema.ENUMS['decision_outcome'],
                                               'municipal_decision.outcome', w),
                               'evidence': evidence(decision.get('evidence'), 'municipal_decision.evidence', w)},
        'appellants': [{'name': text(a.get('name')),
                        **{k: enum(a.get(k), schema.ENUMS[k], f'appellants[{i}].{k}', w)
                           for k in ('relationship', 'seeks', 'representation')},
                        'evidence': evidence(a.get('evidence'), f'appellants[{i}].evidence', w)}
                       for i, a in enumerate(draft.get('appellants') or []) if isinstance(a, dict)],
        'discretion': {'description': text(discretion.get('description')),
                       'involved': enum(discretion.get('involved'), schema.ENUMS['involved'], 'discretion.involved', w),
                       'evidence': evidence(discretion.get('evidence'), 'discretion.evidence', w),
                       'central': enum(discretion.get('central'), schema.ENUMS['central'], 'discretion.central', w),
                       'central_evidence': evidence(discretion.get('central_evidence'), 'discretion.central_evidence', w)},
        'observed_status': {'litigation': enum(status.get('litigation'), schema.ENUMS['litigation'],
                                               'observed_status.litigation', w),
                            'permission': enum(status.get('permission'), schema.ENUMS['permission'],
                                               'observed_status.permission', w),
                            'description': text(status.get('description')),
                            'evidence': evidence(status.get('evidence'), 'observed_status.evidence', w)},
        'timeline': [{'kind': e.get('kind'), 'date': date(e.get('date'), f'timeline[{i}].date', w),
                      'court_or_body': text(e.get('court_or_body')),
                      'dockets': [d for d in e.get('dockets') or [] if isinstance(d, str) and d.strip()],
                      'description': text(e.get('description')),
                      'evidence': evidence(e.get('evidence'), f'timeline[{i}].evidence', w)}
                     for i, e in enumerate(events(draft.get('timeline'), w))],
        'duration': {'start': start, 'end': end,
                     'note': text((bound or {}).get('note')) if bound else 'No step established this interval',
                     'kind': kind, 'days': days},
        'restrictions': [{'description': text(e.get('description')),
                          'start': date(e.get('start'), f'restrictions[{i}].start', w),
                          'end': date(e.get('end'), f'restrictions[{i}].end', w),
                          'evidence': evidence(e.get('evidence'), f'restrictions[{i}].evidence', w)}
                         for i, e in enumerate(draft.get('restrictions') or []) if isinstance(e, dict)],
        'housing_changes': [{'description': text(e.get('description')),
                             'evidence': evidence(e.get('evidence'), f'housing_changes[{i}].evidence', w)}
                            for i, e in enumerate(draft.get('housing_changes') or []) if isinstance(e, dict)],
        'related_dockets': [{'docket': text(e.get('docket')), 'relationship': text(e.get('relationship')),
                             'evidence': evidence(e.get('evidence'), f'related_dockets[{i}].evidence', w)}
                            for i, e in enumerate(draft.get('related_dockets') or []) if isinstance(e, dict)],
    }, w


def records(draft, bounds=None):
    """Contract records for a v6 draft; dockets without a bound have an unknown duration."""
    by_docket = {b.get('docket'): b for b in bounds or []}
    result, warnings = [], []
    for item in (draft or {}).get('dockets', []):
        converted, notes = record(item, by_docket.get(item.get('docket')))
        result.append(converted)
        warnings.extend(f'{converted["docket"]}: {n}' for n in notes)
    return result, warnings
