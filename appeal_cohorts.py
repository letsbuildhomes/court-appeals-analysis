"""Freeze reproducible case selections from a run.

A cohort records both its selection query and the resulting case identities,
with each case's frozen source hash. Re-running the query later may select
different cases; the saved identities are what an experiment uses.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random

from pipeline import runs

COHORT_VERSION = 'appeal-cohort-2'
QUERY_FIELDS = {'case': 'Explicit case identities',
                'status': 'Processing status: complete, failed or in_progress',
                'disposition': 'Disposition: in_scope, uncertain or excluded',
                'duration_kind': 'Any record with this duration kind, e.g. unknown'}


def matches(case, query):
    """All supplied query fields must match; values within a field are alternatives."""
    tests = {'case': lambda: case['case_id'] in query['case'],
             'status': lambda: case['status']['state'] in query['status'],
             'disposition': lambda: (case.get('disposition') or {}).get('value') in query['disposition'],
             'duration_kind': lambda: any((r.get('duration') or {}).get('kind') in query['duration_kind']
                                          for r in case.get('records') or [])}
    return all(tests[field]() for field in QUERY_FIELDS if query.get(field))


def validate_query(query):
    unknown = set(query) - set(QUERY_FIELDS) - {'sample', 'seed'}
    if unknown:
        raise ValueError('Unknown query fields: '+', '.join(sorted(unknown)))
    for field in QUERY_FIELDS:
        values = query.get(field)
        if values is not None and (not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
            raise ValueError(f'Query field {field} must be a list of strings')
    if not any(query.get(field) for field in QUERY_FIELDS):
        raise ValueError('Select at least one query field; a whole-run cohort must be explicit')
    sample = query.get('sample')
    if sample is not None and (not isinstance(sample, int) or isinstance(sample, bool) or sample < 1):
        raise ValueError('Sample size must be a positive integer')
    if (sample is None) != (query.get('seed') is None):
        raise ValueError('A sample requires a seed, and a seed requires a sample')


def select_cases(cases, query):
    """Return matching case identities in run order; sampling is seeded."""
    validate_query(query)
    known = {case['case_id'] for case in cases}
    absent = set(query.get('case') or []) - known
    if absent:
        raise ValueError('Unknown case identities: '+', '.join(sorted(absent)))
    selected = [case['case_id'] for case in cases if matches(case, query)]
    if query.get('sample') is not None and query['sample'] < len(selected):
        chosen = set(random.Random(query['seed']).sample(sorted(selected), query['sample']))
        selected = [case_id for case_id in selected if case_id in chosen]
    return selected


def create_cohort(run, output, query, *, name, description):
    """Write a new cohort file; an existing file is never replaced."""
    run, output = Path(run).resolve(), Path(output)
    loaded = runs.load(run)
    selected = select_cases(loaded['cases'], query)
    if not selected:
        raise ValueError('The query selected no cases')
    by_id = {case['case_id']: case for case in loaded['cases']}
    cohort = {'schema_version': COHORT_VERSION, 'name': name, 'description': description,
              'created_at': datetime.now(timezone.utc).isoformat(),
              'base': {'run': str(run), 'pipeline': loaded['run']['pipeline'], 'case_count': len(loaded['cases'])},
              'query': query, 'count': len(selected),
              'cases': [{'case_id': case_id, 'source_sha256': by_id[case_id]['source']['sha256']}
                        for case_id in selected]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as f:
        json.dump(cohort, f, indent=2, ensure_ascii=False)
        f.write('\n')
    return cohort


def read_cohort(path):
    raw = Path(path).read_bytes()
    cohort = json.loads(raw)
    if cohort.get('schema_version') != COHORT_VERSION:
        raise ValueError('Unsupported cohort version')
    ids = [case['case_id'] for case in cohort['cases']]
    if not ids or len(ids) != len(set(ids)) or len(ids) != cohort['count']:
        raise ValueError('Cohort cases are empty, duplicated or miscounted')
    if not all(isinstance(case.get('source_sha256'), str) for case in cohort['cases']):
        raise ValueError('Every cohort case requires its frozen source hash')
    return {**cohort, 'sha256': hashlib.sha256(raw).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    select = sub.add_parser('select', help='Freeze a query and its selected case identities')
    select.add_argument('output', type=Path, help='New cohort file, e.g. experiments/NAME/cohort.json')
    select.add_argument('--from', dest='run', type=Path, default=runs.RUNS/'production',
                        help='Run to select from (default: data/runs/production)')
    select.add_argument('--name', required=True)
    select.add_argument('--description', required=True, help='Why these cases were selected')
    for field, text in QUERY_FIELDS.items():
        select.add_argument('--'+field.replace('_', '-'), dest=field, action='append', help=text+' (repeatable)')
    select.add_argument('--sample', type=int, help='Seeded random sample size, applied after filtering')
    select.add_argument('--seed', type=int)
    inspect = sub.add_parser('inspect', help='Validate and summarize a cohort')
    inspect.add_argument('cohort', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'select':
            query = {field: getattr(args, field) for field in QUERY_FIELDS if getattr(args, field)}
            if args.sample is not None or args.seed is not None:
                query.update(sample=args.sample, seed=args.seed)
            value = create_cohort(args.run, args.output, query, name=args.name, description=args.description)
        else:
            value = read_cohort(args.cohort)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        parser.error(str(exc))
    print(json.dumps({k: v for k, v in value.items() if k != 'cases'}, indent=2, ensure_ascii=False))
    print('Cases: '+', '.join(case['case_id'] for case in value['cases']))


if __name__ == '__main__':
    main()
