"""The research output contract: one result per case.

A result has a disposition (in scope, excluded with a reason, or uncertain)
and zero or more docket records. Every field declares its allowed values and
an explicit ``unknown``. Evidence is a list of ``{doc, page, quote}`` citations
that must be verifiable against the frozen source text. Processing status
(complete, failed, in progress) is run metadata stored beside each result.

The fact catalog at the end names the research facts that comparisons and the
dashboard measure. Each fact records the schema version that introduced it,
so a result made under an older version shows the fact as not measured rather
than unknown.
"""
from collections import Counter
import re

SCHEMA = 'appeal-result-1'
# Schema versions in order; a fact introduced by a later version is not
# measured by results made under an earlier one.
VERSIONS = ('appeal-result-1',)
UNKNOWN = 'unknown'

DISPOSITIONS = ('in_scope', 'excluded', 'uncertain')
SCOPE = ('yes', 'no', UNKNOWN)
ENUMS = {
    'decision_outcome': ('approved', 'approved_with_conditions', 'denied', 'other', UNKNOWN),
    'relationship': ('developer', 'neighbor', 'organization', 'other', UNKNOWN),
    'seeks': ('overturn_approval', 'overturn_denial', 'change_conditions', 'other', UNKNOWN),
    'representation': ('represented', 'pro_se', UNKNOWN),
    'involved': ('yes', 'no', UNKNOWN),
    'central': ('yes', 'no', 'not_applicable', UNKNOWN),
    'litigation': ('ongoing', 'resolved', UNKNOWN),
    'permission': ('allowed', 'allowed_with_conditions', 'denied', UNKNOWN),
    'event': ('filing', 'judgment', 'further_appeal', 'reopening', 'remand', 'resolution'),
    'start_relation': ('exact', 'on_or_before'),
    'end_relation': ('exact', 'on_or_after'),
    'duration_kind': ('exact', 'at_least', UNKNOWN),
}
DATE = re.compile(r'\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?\Z')
FULL_DATE = re.compile(r'\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\Z')


# Validation ----------------------------------------------------------------
class _Check:
    def __init__(self):
        self.errors = []

    def fail(self, path, message):
        self.errors.append(f'{path}: {message}')

    def keys(self, value, path, required, optional=()):
        if not isinstance(value, dict):
            self.fail(path, 'must be an object')
            return False
        missing = [k for k in required if k not in value]
        extra = sorted(set(value) - set(required) - set(optional))
        if missing:
            self.fail(path, 'missing '+', '.join(missing))
        if extra:
            self.fail(path, 'unexpected '+', '.join(extra))
        return not missing

    def enum(self, value, path, allowed):
        if value not in allowed:
            self.fail(path, f'{value!r} is not one of {", ".join(allowed)}')

    def text(self, value, path):
        if not isinstance(value, str) or not value.strip():
            self.fail(path, 'must be nonempty text or "unknown"')

    def date(self, value, path, full=False):
        if value == UNKNOWN:
            return
        if not isinstance(value, str) or not (FULL_DATE if full else DATE).match(value):
            self.fail(path, 'must be '+('YYYY-MM-DD' if full else 'YYYY, YYYY-MM or YYYY-MM-DD')+' or "unknown"')

    def count(self, value, path):
        if value != UNKNOWN and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            self.fail(path, 'must be a nonnegative integer or "unknown"')

    def evidence(self, value, path):
        if not isinstance(value, list):
            self.fail(path, 'must be a list')
            return
        for i, cite in enumerate(value):
            where = f'{path}[{i}]'
            if self.keys(cite, where, ('doc', 'page', 'quote')):
                for key in ('doc', 'page'):
                    if not isinstance(cite[key], int) or isinstance(cite[key], bool) or cite[key] < 1:
                        self.fail(f'{where}.{key}', 'must be a positive integer')
                if not isinstance(cite['quote'], str) or not cite['quote'].strip():
                    self.fail(f'{where}.quote', 'must be nonempty text')

    def items(self, value, path, check):
        if not isinstance(value, list):
            self.fail(path, 'must be a list')
            return
        for i, item in enumerate(value):
            check(item, f'{path}[{i}]')


def _record(c, r, path):
    fields = ('docket', 'project', 'municipal_decision', 'appellants', 'discretion', 'observed_status', 'timeline',
              'duration', 'restrictions', 'housing_changes', 'related_dockets')
    if not c.keys(r, path, fields):
        return
    c.text(r['docket'], path+'.docket')
    p = r['project']
    if c.keys(p, path+'.project', ('description', 'municipality', 'proposed_units', 'proposed_lots', 'evidence')):
        c.text(p['description'], path+'.project.description')
        c.text(p['municipality'], path+'.project.municipality')
        c.count(p['proposed_units'], path+'.project.proposed_units')
        c.count(p['proposed_lots'], path+'.project.proposed_lots')
        c.evidence(p['evidence'], path+'.project.evidence')
    d = r['municipal_decision']
    if c.keys(d, path+'.municipal_decision', ('description', 'date', 'outcome', 'evidence')):
        c.text(d['description'], path+'.municipal_decision.description')
        c.date(d['date'], path+'.municipal_decision.date')
        c.enum(d['outcome'], path+'.municipal_decision.outcome', ENUMS['decision_outcome'])
        c.evidence(d['evidence'], path+'.municipal_decision.evidence')

    def appellant(a, where):
        if c.keys(a, where, ('name', 'relationship', 'seeks', 'representation', 'evidence')):
            c.text(a['name'], where+'.name')
            for key in ('relationship', 'seeks', 'representation'):
                c.enum(a[key], f'{where}.{key}', ENUMS[key])
            c.evidence(a['evidence'], where+'.evidence')
    c.items(r['appellants'], path+'.appellants', appellant)
    x = r['discretion']
    if c.keys(x, path+'.discretion', ('description', 'involved', 'evidence', 'central', 'central_evidence')):
        c.text(x['description'], path+'.discretion.description')
        c.enum(x['involved'], path+'.discretion.involved', ENUMS['involved'])
        c.enum(x['central'], path+'.discretion.central', ENUMS['central'])
        c.evidence(x['evidence'], path+'.discretion.evidence')
        c.evidence(x['central_evidence'], path+'.discretion.central_evidence')
    s = r['observed_status']
    if c.keys(s, path+'.observed_status', ('litigation', 'permission', 'description', 'evidence')):
        c.enum(s['litigation'], path+'.observed_status.litigation', ENUMS['litigation'])
        c.enum(s['permission'], path+'.observed_status.permission', ENUMS['permission'])
        c.text(s['description'], path+'.observed_status.description')
        c.evidence(s['evidence'], path+'.observed_status.evidence')

    def event(e, where):
        if c.keys(e, where, ('kind', 'date', 'court_or_body', 'dockets', 'description', 'evidence')):
            c.enum(e['kind'], where+'.kind', ENUMS['event'])
            c.date(e['date'], where+'.date')
            c.text(e['court_or_body'], where+'.court_or_body')
            c.items(e['dockets'], where+'.dockets', lambda v, w: c.text(v, w))
            c.text(e['description'], where+'.description')
            c.evidence(e['evidence'], where+'.evidence')
    c.items(r['timeline'], path+'.timeline', event)
    _duration(c, r['duration'], path+'.duration')

    def restriction(e, where):
        if c.keys(e, where, ('description', 'start', 'end', 'evidence')):
            c.text(e['description'], where+'.description')
            c.date(e['start'], where+'.start')
            c.date(e['end'], where+'.end')
            c.evidence(e['evidence'], where+'.evidence')
    c.items(r['restrictions'], path+'.restrictions', restriction)

    def change(e, where):
        if c.keys(e, where, ('description', 'evidence')):
            c.text(e['description'], where+'.description')
            c.evidence(e['evidence'], where+'.evidence')
    c.items(r['housing_changes'], path+'.housing_changes', change)

    def related(e, where):
        if c.keys(e, where, ('docket', 'relationship', 'evidence')):
            c.text(e['docket'], where+'.docket')
            c.text(e['relationship'], where+'.relationship')
            c.evidence(e['evidence'], where+'.evidence')
    c.items(r['related_dockets'], path+'.related_dockets', related)


def _duration(c, d, path):
    if not c.keys(d, path, ('start', 'end', 'note', 'kind', 'days')):
        return
    for side in ('start', 'end'):
        point = d[side]
        if point == UNKNOWN:
            continue
        where = f'{path}.{side}'
        if c.keys(point, where, ('date', 'relation', 'description', 'evidence')):
            c.date(point['date'], where+'.date', full=True)
            if point['date'] == UNKNOWN:
                c.fail(where+'.date', 'an endpoint needs a date; use "unknown" for the whole endpoint')
            c.enum(point['relation'], where+'.relation', ENUMS[side+'_relation'])
            c.text(point['description'], where+'.description')
            c.evidence(point['evidence'], where+'.evidence')
    c.text(d['note'], path+'.note')
    c.enum(d['kind'], path+'.kind', ENUMS['duration_kind'])
    c.count(d['days'], path+'.days')
    if not c.errors and (d['kind'], d['days']) != measure(d['start'], d['end']):
        c.fail(path, 'kind and days must be calculated from start and end')


def validate(result):
    """Return a list of contract violations; empty when the result is valid."""
    c = _Check()
    if not c.keys(result, 'result', ('schema', 'case', 'disposition', 'records')):
        return c.errors
    if result['schema'] != SCHEMA:
        c.fail('result.schema', f'must be {SCHEMA}')
    c.text(result['case'], 'result.case')
    d = result['disposition']
    if d is not None and c.keys(d, 'disposition', ('value', 'reason', 'municipal_appeal', 'housing', 'evidence')):
        c.enum(d['value'], 'disposition.value', DISPOSITIONS)
        c.text(d['reason'], 'disposition.reason')
        if d['value'] == 'excluded' and d['reason'] == UNKNOWN:
            c.fail('disposition.reason', 'an exclusion needs a reason')
        c.enum(d['municipal_appeal'], 'disposition.municipal_appeal', SCOPE)
        c.enum(d['housing'], 'disposition.housing', SCOPE)
        c.evidence(d['evidence'], 'disposition.evidence')
    c.items(result['records'], 'records', lambda r, where: _record(c, r, where))
    if d is None and result['records']:
        c.fail('records', 'records need a disposition')
    if d is not None and d['value'] == 'excluded' and result['records']:
        c.fail('records', 'an excluded case has no records')
    return c.errors


def result(case_id, disposition, records=()):
    return {'schema': SCHEMA, 'case': case_id, 'disposition': disposition, 'records': list(records)}


# Duration arithmetic -------------------------------------------------------
def measure(start, end):
    """The (kind, days) an interval establishes; a zero-day minimum establishes nothing."""
    from datetime import date
    if start == UNKNOWN or end == UNKNOWN:
        return UNKNOWN, UNKNOWN
    days = (date.fromisoformat(end['date'])-date.fromisoformat(start['date'])).days
    if days < 0:
        return UNKNOWN, UNKNOWN
    kind = 'exact' if start['relation'] == end['relation'] == 'exact' else 'at_least'
    if days == 0 and kind == 'at_least':
        return UNKNOWN, UNKNOWN
    return kind, days


# Evidence verification ------------------------------------------------------
def citations(value, path=''):
    """Every (path, citation) in a result, depth first."""
    if isinstance(value, dict):
        if {'doc', 'page', 'quote'} <= set(value):
            yield path, value
            return
        for key, child in value.items():
            yield from citations(child, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from citations(child, f'{path}[{i}]')


def verify_quotes(result, pages):
    """Check each citation against the frozen source; return (checked, unverified paths)."""
    from group_scout import quote_on_page
    checked, unverified = 0, []
    for path, cite in citations({k: result[k] for k in ('disposition', 'records')}):
        checked += 1
        text = pages.get(cite.get('doc'), {}).get(cite.get('page'))
        following = pages.get(cite.get('doc'), {}).get((cite.get('page') or 0)+1, '')
        if text is None or not quote_on_page(cite['quote'], text, following):
            unverified.append(path)
    return checked, unverified


# Fact catalog --------------------------------------------------------------
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


def _duration_value(record):
    if isinstance(record.get('duration'), int) and not isinstance(record.get('duration'), bool):
        return f'{record["duration"]} days'  # A bare day count, as other producers may record it.
    kind, days = _get(record, 'duration', 'kind'), _get(record, 'duration', 'days')
    if kind not in ('exact', 'at_least') or not isinstance(days, int):
        return None
    return f'{days} days' if kind == 'exact' else f'≥ {days} days'


def _endpoint(side):
    def value(record):
        point = _get(record, 'duration', side)
        if not isinstance(point, dict) or not point.get('date') or point['date'] == UNKNOWN:
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
    values = sorted(str(a.get('representation') or UNKNOWN) for a in record.get('appellants') or [] if isinstance(a, dict))
    if not values or all(v == UNKNOWN for v in values):
        return None
    return ', '.join(f'{n} {v}' for v, n in sorted(Counter(values).items()))


def _related(record):
    dockets = sorted({' '.join(str(d.get('docket')).split()) for d in record.get('related_dockets') or []
                      if isinstance(d, dict) and d.get('docket') not in (None, UNKNOWN)})
    return ', '.join(dockets) if dockets else None


def _municipality(value):
    """'Town of Jericho' and 'Jericho, Vermont' name the same place."""
    text = re.sub(r'^(town|city|village) of\s+', '', str(value).strip().casefold())
    return re.sub(r',?\s*(vermont|vt\.?)$', '', text).strip()


def _field(*path):
    return lambda record: _get(record, *path)


SPECIFICITY = {UNKNOWN: 0, 'at_least': 1, 'exact': 2}


def fact(key, question, label, value, evidence, rank=None, normalize=None, since=SCHEMA):
    return {'key': key, 'question': question, 'label': label, 'value': value, 'evidence': evidence,
            'rank': rank, 'normalize': normalize, 'since': since}


RECORD_FACTS = [
    fact('duration', 'Duration', 'Appeal duration', _duration_value,
         lambda r: _evidence(_get(r, 'duration', 'start', 'evidence'), _get(r, 'duration', 'end', 'evidence')),
         rank=lambda r: SPECIFICITY.get(_get(r, 'duration', 'kind'), 0)),
    fact('start', 'Duration', 'Start date', _endpoint('start'), _object_evidence('duration', 'start', 'evidence'),
         rank=lambda r: 2 if _get(r, 'duration', 'start', 'relation') == 'exact' else 1),
    fact('end', 'Duration', 'End date', _endpoint('end'), _object_evidence('duration', 'end', 'evidence'),
         rank=lambda r: 2 if _get(r, 'duration', 'end', 'relation') == 'exact' else 1),
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
    fact('disposition', 'Scope', 'Disposition', _field('disposition', 'value'), _object_evidence('disposition', 'evidence')),
    fact('municipal_appeal', 'Scope', 'Municipal appeal', _field('disposition', 'municipal_appeal'),
         _object_evidence('disposition', 'evidence')),
    fact('housing', 'Scope', 'Housing', _field('disposition', 'housing'), _object_evidence('disposition', 'evidence')),
]
QUESTIONS = ['Scope', 'Duration', 'Decision & status', 'Discretion', 'Appellants', 'Project', 'Effects', 'Links', 'Other fields']


def measures(spec, schema):
    """Whether a result made under ``schema`` measures this fact."""
    return schema in VERSIONS and VERSIONS.index(schema) >= VERSIONS.index(spec['since'])
