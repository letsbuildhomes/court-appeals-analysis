"""Read-only canonical case boundary for frozen historical appeals runs.

This boundary preserves drafts and execution coverage separately from finalized
docket records. It does not import pipeline configuration or call any models.
"""
import copy
import fcntl
import hashlib
import json
import re
from pathlib import Path
from pipeline import recipe as recipes
from pipeline.components import load_file
from appeal_assembly import effective_status, finalized_dockets, latest_checkpoint

CONTRACT_VERSION = 'appeal-cases-1'
_SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def scope_category(scope, route):
    values = [scope.get(k) for k in ('municipal_appeal', 'housing')]
    if route in ('out_of_scope', 'no_members') or 'no' in values:
        return 'excluded'
    return 'confirmed' if values == ['yes', 'yes'] else 'uncertain'


def _paths(folder, manifest):
    paths = [folder/'manifest.json']
    for name in ('prompts.json', 'recipe.json'):
        if (folder/name).exists():
            paths.append(folder/name)
    paths.extend(sorted((folder/'components').glob('*.py')))
    paths.extend(sorted((folder/'calls').rglob('*.result.json')))
    seen = set()
    for group in manifest['groups']:
        gid = group['group_id']
        if not isinstance(gid, str) or not _SAFE.fullmatch(gid) or gid in seen:
            raise ValueError('Invalid or duplicate group identity')
        seen.add(gid)
        source = folder/'sources'/f'{gid}.txt'
        if source.exists():
            paths.append(source)
        for sub in ('results', 'first_pass', 'activity'):
            path = folder/sub/f'{gid}.json'
            if path.exists():
                paths.append(path)
    return paths


def adapt_run(folder: Path, verify_sources=True) -> dict:
    """Adapt appeals-1 under its existing shared lock, without creating files.

    Missing/mismatched finalized bounds fail explicitly. ``verify_sources=False``
    permits source-free archives, recording verification as disabled; input
    hashes and stability checks still cover all available frozen inputs.
    """
    folder = Path(folder)
    try:
        lock = (folder/'.lock').open('rb')
    except FileNotFoundError:
        raise ValueError('Selected run has no pipeline lock') from None
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Selected run is being written') from None
        return _adapt_locked(folder, verify_sources)


def _adapt_locked(folder, verify_sources):
    manifest_raw = (folder/'manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get('schema_version') != 'appeals-1':
        raise ValueError('Unsupported pipeline manifest version')
    paths = _paths(folder, manifest)
    raw = {p: p.read_bytes() for p in paths}
    times = {p: p.stat().st_mtime_ns for p in paths}
    if raw[folder/'manifest.json'] != manifest_raw:
        raise ValueError('Manifest changed while adapting run')
    prompts_path = folder/'prompts.json'
    if verify_sources:
        if prompts_path not in raw or _sha(json.dumps(json.loads(raw[prompts_path]), sort_keys=True).encode()) != manifest.get('prompt_sha256'):
            raise ValueError('Frozen prompts missing or changed')
    cases = []
    for group in manifest['groups']:
        gid = group['group_id']
        source_path = folder/'sources'/f'{gid}.txt'
        if verify_sources and (source_path not in raw or _sha(raw[source_path]) != group.get('source_sha256')):
            raise ValueError('Frozen source missing or changed: '+gid)
        available = [folder/sub/f'{gid}.json' for sub in ('results', 'first_pass') if folder/sub/f'{gid}.json' in raw]
        selected = latest_checkpoint(available, times)
        result = json.loads(raw[selected]) if selected else {'status': 'not_run'}
        checkpoint_status = result.get('status', 'not_run')
        status = checkpoint_status
        activity_path = folder/'activity'/f'{gid}.json'
        activity = json.loads(raw[activity_path]) if activity_path in raw else None
        status = effective_status(status, checkpoint_time=times.get(selected), activity_time=times.get(activity_path), activity_stage=(activity or {}).get('stage'), eligible=group.get('eligible', True))
        scope = result.get('triage') or {}
        route = result.get('route') or {}
        candidate = result.get('candidate')
        bounds = result.get('duration_bounds') or []
        records = []
        if status == 'complete' and candidate:
            if candidate.get('schema_version') != 'v6':
                raise ValueError('Only finalized v6 extraction records are supported')
            drafts = candidate.get('dockets', [])
            ids = [r.get('docket') for r in drafts]
            bound_ids = [b.get('docket') for b in bounds]
            if len(set(ids)) != len(ids) or len(set(bound_ids)) != len(bound_ids) or set(ids) != set(bound_ids):
                raise ValueError('Completed dockets require exactly matching finalized duration bounds')
            for b in bounds:
                if 'duration' not in b:
                    raise ValueError('Finalized duration bound missing duration')
            records = copy.deepcopy(finalized_dockets(drafts, bounds, strict=True))
        cases.append({'case_id': gid, 'group_id': gid,
            'source': {'path': str(source_path.relative_to(folder)), 'sha256': group.get('source_sha256'), 'documents': copy.deepcopy(group.get('documents', [])), 'dockets': copy.deepcopy(group.get('dockets', []))},
            'execution': {'status': status, 'checkpoint_status': checkpoint_status, 'checkpoint_path': str(selected.relative_to(folder)) if selected else None, 'eligible': group.get('eligible', True), 'activity': activity, 'error': result.get('error'), 'error_type': result.get('error_type'), 'warnings': result.get('warnings', [])},
            'scope': scope, 'scope_category': scope_category(scope, route.get('route')), 'route': route,
            'candidate': candidate, 'review': result.get('review'), 'duration_bounds': bounds, 'records': records})
    if _paths(folder, manifest) != paths or any(p.read_bytes() != raw[p] or p.stat().st_mtime_ns != times[p] for p in paths):
        raise ValueError('Run inputs changed while adapting run')
    calls = [json.loads(raw[p]) for p in paths if p.parent != folder and 'calls' in p.relative_to(folder).parts and p.name.endswith('.result.json')]
    charges = [c['charged_or_reserved_usd'] for c in calls if isinstance(c.get('charged_or_reserved_usd'), (int, float)) and not isinstance(c.get('charged_or_reserved_usd'), bool)]
    cost = {'kind': 'inherited', 'recorded_usd': sum(charges) if charges else None,
            'completeness': 'reported' if calls and len(charges) == len(calls) else 'partial' if charges else 'unknown'}
    prompts = json.loads(raw[prompts_path]) if prompts_path in raw else None
    # Runs made before recipes are described from their manifest settings.
    for path in paths:
        if path.parent == folder/'components':
            load_file(path)
    recipe = (json.loads(raw[folder/'recipe.json']) if folder/'recipe.json' in raw
              else recipes.legacy_recipe(manifest, prompts or {}))
    return {'schema_version': CONTRACT_VERSION,
        'run': {'name': manifest.get('name'), 'created_at': manifest.get('created_at'), 'pipeline_schema': 'appeals-1'},
        'recipe': recipes.describe(recipe),
        'prompts': prompts,
        'cases': cases,
        'provenance': {'adapter_version': CONTRACT_VERSION, 'historical_code_revision': None, 'sources_verified': bool(verify_sources), 'frozen_prompts': {'path': 'prompts.json', 'sha256': _sha(raw[prompts_path])} if prompts_path in raw else None, 'cost': cost, 'input_hashes': [{'file': str(p.relative_to(folder)), 'sha256': _sha(raw[p]), 'mtime_ns': times[p]} for p in paths]}}
