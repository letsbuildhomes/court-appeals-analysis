"""Pure checkpoint selection and finalized-record assembly primitives."""
from collections import Counter
import hashlib
import json


def run_progress(run_dir):
    """Checkpoint status counts and recorded charges for a run directory."""
    counts = Counter()
    manifest = run_dir/'manifest.json'
    for group in json.loads(manifest.read_text())['groups'] if manifest.exists() else []:
        for sub in ('results', 'first_pass'):
            path = run_dir/sub/f'{group["group_id"]}.json'
            if path.exists():
                counts[json.loads(path.read_text())['status']] += 1
                break
        else:
            counts['not_run'] += 1
    charges = sum(json.loads(p.read_text()).get('charged_or_reserved_usd') or 0
                  for p in (run_dir/'calls').rglob('*.result.json')) if (run_dir/'calls').is_dir() else 0
    return {'statuses': dict(counts), 'charged_usd': charges}


def latest_checkpoint(candidates, times):
    """Select newest available candidate; input order breaks timestamp ties."""
    available = [candidate for candidate in candidates if candidate in times]
    return max(available, key=lambda candidate: times[candidate]) if available else None


def effective_status(status, *, checkpoint_time=None, activity_time=None,
                     activity_stage=None, running=False, eligible=True):
    """Overlay newer active work and source eligibility on checkpoint status."""
    if activity_time is not None and (checkpoint_time is None or activity_time > checkpoint_time):
        if activity_stage in ('triaging', 'extracting', 'reviewing', 'measuring'):
            status = activity_stage if running else 'interrupted'
    return status if eligible else 'too_large'


def first_pass_status(record, status):
    """Keep first-pass outcomes independent of subsequent review and duration work."""
    if record.get('status') in {'extracted','reviewed','complete','review_failed','duration_failed','scope_flagged'}:
        route = record.get('route',{}).get('route')
        if route == 'extract':return 'retained'
        if route in {'out_of_scope','no_members'}:return 'excluded'
        return 'issues'
    if status in {'triaging','extracting'}:return 'active'
    if status in {'incomplete','interrupted','too_large'}:return 'issues'
    return 'waiting'


def finalized_dockets(records, bounds, *, missing='raise', strict=False):
    """Remove draft annotations and attach finalized duration evidence.

    Strict snapshots reject duplicate keys and require exact matching for retained
    records. Viewer callers may skip missing bounds; exports retain KeyError.
    """
    if missing not in ('raise', 'skip'):
        raise ValueError('Unknown missing-duration policy')
    by_docket = {(bound.get('docket') if strict else bound['docket']): bound for bound in bounds}
    if strict:
        if len(by_docket) != len(bounds):
            raise ValueError('Duplicate duration docket')
        ids = [record.get('docket') for record in records]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate docket in completed group')
        if records and set(by_docket) != set(ids):
            raise ValueError('Completed dockets require exactly matching finalized duration bounds')
    assembled = []
    for record in records:
        docket = record.get('docket') if strict else record['docket']
        if missing == 'skip' and docket not in by_docket:
            continue
        bound = by_docket[docket]
        clean = {k:v for k,v in record.items() if k not in ('scouting','uncertainties','suggestions')}
        clean['duration_bounds'] = {k:v for k,v in bound.items() if k not in ('docket','duration')}
        clean['duration'] = bound.get('duration') if strict else bound['duration']
        assembled.append(clean)
    return assembled


def execution_recipe(manifest):
    """Describe the current fixed execution topology without driving execution."""
    settings = {key: manifest.get(key) for key in
                ('reasoning', 'max_output_tokens', 'max_chars',
                 'review_batch_size', 'review_batch_chars', 'workers')}
    stages = ['triage', 'extract', 'review', 'duration']
    hashes = manifest.get('prompt_hashes') or {}
    dependencies = {
        'triage': ['system', 'triage'],
        'extract': ['system', 'triage', 'extract'],
        'review': ['primer', 'extract', 'triage', 'review'],
        'duration': ['primer', 'extract', 'triage', 'review', 'duration'],
    }
    def component(stage):
        selected = {name: hashes.get(name) for name in dependencies[stage]}
        fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest() if all(selected.values()) else None
        config = {'model': manifest.get('model'), **settings}
        if stage in ('review', 'duration') and manifest.get('review_max_output_tokens') is not None:
            config['max_output_tokens'] = manifest['review_max_output_tokens']
        return {'id': stage, 'label': stage.title(),
                'config': config,
                'prompt_sha256': hashes.get(stage),
                'prompt_dependencies': selected,
                'effective_prompt_sha256': fingerprint}
    return {'components': [component(stage) for stage in stages],
            'edges': [{'from': start, 'to': end} for start, end in zip(stages, stages[1:])],
            'stages': stages,
            'finishing': manifest.get('finishing', 'review-duration-followup'),
            'model': manifest.get('model'),
            'prompt_sha256': manifest.get('prompt_sha256'),
            'settings': settings}
