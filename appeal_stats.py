"""Live research summaries at docket grain, preserving missing observations."""
import math


def research_stats(cases, records):
    completed = {c['group']: c for c in cases if c['status'] == 'complete'}
    # Archive mode already selects one run per group. Count each docket once.
    unique = {(r['group'], r['record']['docket']): r for r in records
              if r['group'] in completed}
    confirmed, uncertain = [], []
    for row in unique.values():
        case = completed[row['group']]
        scope = case.get('scope', {})
        if case.get('route', {}).get('route') in ('out_of_scope', 'no_members'):
            continue
        values = [scope.get(k) for k in ('municipal_appeal', 'housing')]
        if values == ['yes', 'yes']:
            confirmed.append(row)
        elif 'no' not in values:
            uncertain.append(row)

    def ref(row):
        return {'group': row['group'], 'docket': row['record']['docket']}

    def categories(keys):
        return {key: [] for key in keys}

    duration = categories(('exact', 'at_least', 'unknown'))
    discretion = categories(('yes', 'no', 'unclear'))
    central = categories(('yes', 'no', 'unclear'))
    permission = categories(('allowed', 'allowed_with_conditions', 'denied', 'unclear'))
    representation = categories(('represented', 'pro_se', 'mixed', 'unclear'))
    impacts = {key: categories(('documented', 'none_documented', 'unknown'))
               for key in ('housing_changes', 'restrictions')}
    limits = (90, 180, 365, 730, 1460, math.inf)
    buckets = [{'exact': [], 'at_least': []} for _ in limits]
    supported_days, exact_days = [], []

    for row in confirmed:
        record, member = row['record'], ref(row)
        measured = record.get('duration') or {}
        days, kind = measured.get('days'), measured.get('kind')
        valid = (kind in ('exact', 'at_least') and type(days) in (int, float)
                 and math.isfinite(days) and days >= 0)
        duration[kind if valid else 'unknown'].append(member)
        if valid:
            supported_days.append(days)
            if kind == 'exact':
                exact_days.append(days)
            index = next(i for i, upper in enumerate(limits) if days <= upper)
            buckets[index][kind].append(member)

        reason = record.get('discretion') or {}
        involved = reason.get('involved')
        discretion[involved if involved in discretion else 'unclear'].append(member)
        if involved == 'yes':
            primary = reason.get('central')
            central[primary if primary in central else 'unclear'].append(member)

        outcome = (record.get('observed_status') or {}).get('permission')
        permission[outcome if outcome in permission else 'unclear'].append(member)
        for field, groups in impacts.items():
            findings = record.get(field)
            key = ('documented' if findings else 'none_documented') if isinstance(findings, list) else 'unknown'
            groups[key].append(member)

        parties = record.get('appellants')
        statuses = {p.get('representation') for p in parties} if isinstance(parties, list) else set()
        if not statuses or not statuses <= {'represented', 'pro_se'}:
            key = 'unclear'
        elif len(statuses) == 2:
            key = 'mixed'
        else:
            key = next(iter(statuses))
        representation[key].append(member)

    return {
        'appeals': [ref(row) for row in confirmed],
        'groups': len({row['group'] for row in confirmed}),
        'uncertain_scope': [ref(row) for row in uncertain],
        'review_flagged': [ref(row) for row in confirmed if completed[row['group']].get('review_required')],
        'duration': {**duration, 'mean_supported_days': sum(supported_days) / len(supported_days) if supported_days else None,
                     'mean_exact_days': sum(exact_days) / len(exact_days) if exact_days else None,
                     'buckets': buckets},
        'discretion': discretion, 'centrality': central, 'permission': permission,
        'impacts': impacts, 'representation': representation,
    }
