"""Plan, run and finalize experiments defined as override directories.

An experiment definition lives in ``experiments/NAME/``:

    experiment.json   hypothesis, base run, cohort file, and options
    cohort.json       the frozen case selection (``appeal_cohorts.py select``)
    recipe.json       optional recipe fragment composed over the base run's recipe
    prompts/*.md      optional replacement or added prompts, by name
    components/*.py   optional new step components

``plan`` composes the definition over the base run's recipe, freezes the result
into a new run under ``data/experiments/NAME/run``, and estimates spending with a
dry run; it reads no credentials and makes no model calls. ``run`` executes it
under a cap on new spending, reusing any base-run call whose request is
identical. ``finalize`` publishes cohort-restricted baseline and candidate
artifacts with a comparison. ``compare`` records two existing runs as a
retrospective experiment. The base run is only read.
"""
import argparse
from collections import Counter
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import appeals
from appeal_assembly import run_progress
from appeal_cohorts import read_cohort
from appeal_evaluation import evaluate
from appeal_experiments import create_experiment, read_experiment
from appeal_impact import impact
from appeal_runtime import digest, read, save
from config import ROOT
from pipeline import batching, recipe as recipes
from pipeline.engine import Engine, clients_for

PLAN_VERSION = 'appeal-run-experiment-2'
DEFINITION_VERSION = 'appeal-experiment-definition-1'
EXPERIMENTS = ROOT/'data/experiments'
DEFINITIONS = ROOT/'experiments'
REVIEW_CALL = re.compile(r'review-[0-9a-f]{16}\Z')


def code_revision():
    def git(*command):
        return subprocess.run(['git', *command], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    try:
        return {'commit': git('rev-parse', 'HEAD'),
                'dirty': bool(git('status', '--porcelain', '--untracked-files=no'))}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': None}


def charges(paths, inherited=None):
    """Sum recorded charges; ``inherited`` selects only inherited (True) or new (False) calls."""
    total = 0
    for path in paths:
        state = read(path)
        if inherited is None or bool(state.get('inherited_from')) == inherited:
            total += state.get('charged_or_reserved_usd') or 0
    return total


def review_batches(calls, recursive=False):
    """Review membership, read from the requests actually sent (not JSON repairs)."""
    batches = []
    for path in sorted(calls.rglob('*.request.json') if recursive else calls.glob('*.request.json')):
        name = path.name[:-len('.request.json')]
        if REVIEW_CALL.fullmatch(name):
            cases = json.loads(read(path)['messages'][1]['content'])['cases']
            batches.append((name, [case['group'] for case in cases]))
    return batches


def case_charges(calls, groups):
    """Recorded charges for these groups' calls, including retried attempts."""
    groups = set(groups)
    batches = {name[len('review-'):] for name, members in review_batches(calls, recursive=True) if groups & set(members)}
    total = 0
    for path in calls.rglob('*.result.json'):
        name = path.name[:-len('.result.json')]
        if name.startswith(('review-', 'duration-')):
            owned = name.split('-')[1].split('.')[0] in batches
        else:
            owned = name.split('.')[0] in groups
        if owned:
            total += read(path).get('charged_or_reserved_usd') or 0
    return total


def new_folder(root, name):
    if not appeals.SAFE.fullmatch(name) or (root/name).exists():
        raise ValueError('Choose a new experiment name using letters, numbers, dots, dashes or underscores')
    return root/name


def publish(folder, build):
    """Assemble an experiment beside its destination, then publish it in one rename."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+folder.name+'-', dir=folder.parent))
    try:
        build(temporary)
        temporary.rename(folder)
    except BaseException:
        shutil.rmtree(temporary)
        raise


def load_run(folder):
    manifest, prompts = read(folder/'manifest.json'), read(folder/'prompts.json')
    return manifest, prompts, recipes.for_run(folder, manifest, prompts)


def variables(base_recipe, base_prompts, recipe, prompts, components=()):
    """What a recipe changes relative to its base, for records and the dashboard."""
    def strip(value):
        return {k: v for k, v in value.items() if k not in ('prompts', 'legacy')}
    changed_prompts = {name: {'base_sha256': digest(base_prompts[name]) if name in base_prompts else None,
                              'sha256': digest(prompts[name]) if name in prompts else None}
                       for name in sorted(set(base_prompts) | set(prompts)) if base_prompts.get(name) != prompts.get(name)}
    return {'recipe': recipes.diff(strip(recipes.resolved(base_recipe)), strip(recipes.resolved(recipe))),
            'prompts': changed_prompts,
            'components': [Path(p).name for p in components]}


def batch_of(recipe):
    return recipe['phases'][recipe['order'][1]].get('batch', {})


# Definitions ---------------------------------------------------------------
def init(args):
    """Scaffold a definition directory for a new experiment."""
    folder = args.definitions/args.name
    if not appeals.SAFE.fullmatch(args.name) or folder.exists():
        raise ValueError('Choose a new experiment name using letters, numbers, dots, dashes or underscores')
    base = args.base_run.resolve()
    read(base/'manifest.json')
    try:
        base_ref = str(base.relative_to(ROOT))
    except ValueError:
        base_ref = str(base)
    folder.mkdir(parents=True)
    save(folder/'experiment.json', {'schema_version': DEFINITION_VERSION, 'hypothesis': args.hypothesis,
                                    'base_run': base_ref, 'cohort': 'cohort.json', 'companions': True,
                                    'rerun': [], 'allow_batch_change': False})
    (folder/'README.md').write_text(f'# {args.name}\n\n{args.hypothesis}\n\n'
                                    '## Why\n\n## What changes\n\n## How to judge it\n')
    return folder


def read_definition(folder):
    definition = read(Path(folder)/'experiment.json')
    if definition.get('schema_version') != DEFINITION_VERSION:
        raise ValueError('Unsupported experiment definition version')
    for key in ('hypothesis', 'base_run', 'cohort'):
        if not definition.get(key):
            raise ValueError(f'experiment.json needs {key}')
    unknown = set(definition) - {'schema_version', 'hypothesis', 'base_run', 'cohort', 'companions', 'rerun',
                                 'allow_batch_change', 'notes'}
    if unknown:
        raise ValueError('Unknown experiment.json keys: '+', '.join(sorted(unknown)))
    return definition


def definition_files(folder):
    """Every file in a definition directory with its hash, for provenance."""
    folder = Path(folder)
    return {str(p.relative_to(folder)): digest(p.read_text()) for p in sorted(folder.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


# Planning -------------------------------------------------------------------
def dry_run(run_dir, recipe, prompts, manifest, inherit, rerun, multiplier):
    """Run the recipe without sending; return the call ledger and checkpoints."""
    scratch = Path(tempfile.mkdtemp(prefix='dry-run-'))
    try:
        groups = [{**g, 'source': (run_dir/'sources'/f'{g["group_id"]}.txt').read_text()} for g in manifest['groups']]
        clients = clients_for(scratch, recipe, float('inf'), multiplier, inherit=inherit, dry_run=True)
        engine = Engine(scratch, recipe, prompts, {**manifest, 'groups': groups}, clients, workers=4, rerun=rerun)
        with contextlib.redirect_stdout(io.StringIO()):
            outputs = engine.run_first_pass()
            engine.finish(outputs)
        ledger = [entry for client in engine.all_clients() for entry in client.ledger]
        first = {gid: read(scratch/'first_pass'/f'{gid}.json') for gid in outputs}
        results = {p.stem: read(p) for p in (scratch/'results').glob('*.json')}
        return ledger, first, results, groups, clients
    finally:
        shutil.rmtree(scratch)


def estimate(recipe, prompts, groups, ledger, first, results, clients, multiplier):
    """New calls from the dry run, plus conservative bounds for work it could not reach."""
    first_steps, batch_steps = (recipe['phases'][p]['steps'] for p in recipe['order'])
    counts, upper, inherited = Counter(), 0.0, 0.0
    for entry in ledger:
        step = next((s for s in first_steps if entry['name'].endswith('.'+s)), None) or \
            next((s for s in batch_steps if entry['name'].startswith(s+'-')), entry['name'])
        if entry['kind'] == 'new':
            counts[step] += 1
            upper += entry['usd']
        else:
            inherited += entry['usd']
    sizes = {g['group_id']: len(g['source'].encode()) for g in groups}

    def unreached(step_id, known):
        spec = recipes.step_spec(recipe, step_id)
        tokens = spec['settings'].get('max_output_tokens', 0)
        size = known + sum(len(prompts[n].encode()) for n in spec.get('prompts', {}).values())
        counts[step_id+' (unreached)'] += 1
        return clients[spec['provider']].provider.estimate(size, 4, 2*tokens, tokens)*multiplier
    # A case stopped by a new first-pass call cannot show the requests after it.
    for gid, checkpoint in first.items():
        failed = checkpoint.get('failed_call') if checkpoint.get('status') == 'incomplete' else None
        if failed:
            for step_id in first_steps[first_steps.index(failed.split('.')[-1])+1:]+batch_steps:
                upper += unreached(step_id, sizes[gid])
    # A batch stopped at one step cannot show the later steps' requests.
    stopped = {}
    for gid, result in results.items():
        if result.get('status', '').endswith('_failed'):
            stopped.setdefault(result.get('error') or '', []).append(gid)
    for call, members in stopped.items():
        step = next((s for s in batch_steps if call.startswith(s+'-')), None)
        if step:
            for step_id in batch_steps[batch_steps.index(step)+1:]:
                upper += unreached(step_id, sum(sizes[gid] for gid in members))
    return {'new_calls': dict(counts), 'upper_bound_usd': round(upper, 6), 'inherited_usd': round(inherited, 6),
            'price_multiplier': multiplier,
            'notes': ['The bound prices calls the dry run reached at their exact requests, and later steps it could '
                      'not reach as if every unknown answer used its full output allowance. It excludes JSON-repair '
                      'calls and retries, which the spending cap still governs.']}


def plan(args):
    folder = new_folder(args.root, args.name)
    definition_dir = args.definitions/args.name
    definition = read_definition(definition_dir)
    base = (ROOT/definition['base_run']).resolve()
    base_manifest, base_prompts, base_recipe = load_run(base)
    composed, prompts, components, sources = recipes.compose(base_recipe, base_prompts, definition_dir)
    composed.pop('legacy', None)
    recipes.validate(composed)
    rerun = list(definition.get('rerun', []))
    if set(rerun) - set(composed['steps']):
        raise ValueError('rerun names unknown steps: '+', '.join(sorted(set(rerun) - set(composed['steps']))))
    cohort = read_cohort(definition_dir/definition['cohort'])
    base_groups = {g['group_id']: g for g in base_manifest['groups']}
    for case in cohort['cases']:
        group = base_groups.get(case['case_id'])
        if group is None or group['source_sha256'] != case['source_sha256']:
            raise ValueError(f'Cohort case {case["case_id"]} is absent from the base run or has a different source')

    # Batched steps see their whole batch; keep a cohort case's base companions
    # unless the experiment changes batching on purpose.
    selected = [case['case_id'] for case in cohort['cases']]
    membership = {gid: members for _, members in review_batches(base/'calls') for gid in members}
    same_batching = batch_of(composed) == batch_of(base_recipe)
    wanted = set(selected)
    if definition.get('companions', True) and same_batching:
        wanted |= {gid for s in selected for gid in membership.get(s, [])}
    order = [g['group_id'] for g in base_manifest['groups'] if g['group_id'] in wanted]
    companions = [gid for gid in order if gid not in selected]
    for gid in order:
        if digest((base/'sources'/f'{gid}.txt').read_text()) != base_groups[gid]['source_sha256']:
            raise ValueError('Base frozen source changed: '+gid)
    changes = variables(base_recipe, base_prompts, composed, prompts, components)
    manifest = {'schema_version': 'appeals-1', 'name': args.name, 'created_at': datetime.now(timezone.utc).isoformat(),
                'model': composed['providers'].get('default', {}).get('model'),
                'max_chars': base_manifest['max_chars'],
                **({'cap_changes': base_manifest['cap_changes']} if 'cap_changes' in base_manifest else {}),
                'prompt_sha256': digest(json.dumps(prompts, sort_keys=True)),
                'recipe_sha256': recipes.sha256(composed), 'groups': [base_groups[gid] for gid in order]}

    def build(temporary):
        run_dir = temporary/'run'
        (run_dir/'sources').mkdir(parents=True)
        (run_dir/'.lock').touch()
        for gid in order:
            shutil.copy2(base/'sources'/f'{gid}.txt', run_dir/'sources'/f'{gid}.txt')
        save(run_dir/'prompts.json', prompts)
        save(run_dir/'manifest.json', manifest)
        recipes.freeze(run_dir, composed, components)
        shutil.copytree(definition_dir, temporary/'definition', ignore=shutil.ignore_patterns('__pycache__'))
        ledger, first, results, groups, clients = dry_run(run_dir, composed, prompts, manifest, base/'calls', rerun,
                                                          args.price_multiplier)
        # Compare the batches the dry run formed with the base run's.
        retained = [{**g, **first[g['group_id']]} for g in groups if first[g['group_id']].get('status') == 'extracted'
                    and first[g['group_id']].get('candidate') is not None]
        reached = all(first[gid].get('status') in ('extracted', 'complete', 'too_large') for gid in order)
        formed = [[c['group'] for c in batch] for batch in batching.batches(retained, batch_of(composed))] if reached else None
        if not same_batching or not definition.get('companions', True):
            status = 'changed'
        elif formed is None:
            status = 'verify_after_run'
        else:
            status = 'changed' if any(set(membership.get(g, [])) != set(b) for b in formed for g in b) else 'preserved'
        if status == 'changed' and same_batching and not definition.get('allow_batch_change'):
            raise ValueError('Review batch membership would differ from the base run; keep companions, or set '
                             '"allow_batch_change": true to record batching as a changed variable')
        record = {'schema_version': PLAN_VERSION, 'kind': 'experiment', 'name': args.name,
                  'created_at': manifest['created_at'], 'hypothesis': definition['hypothesis'],
                  'code_revision': code_revision(),
                  'definition': {'path': str(definition_dir), 'files': definition_files(definition_dir), 'sources': sources},
                  'base': {'run': str(base), 'name': base_manifest['name'],
                           'manifest_sha256': digest((base/'manifest.json').read_text()),
                           'recipe_sha256': recipes.sha256(base_recipe)},
                  'cohort': {'path': str((definition_dir/definition['cohort']).resolve()), 'sha256': cohort['sha256'],
                             'name': cohort['name'], 'description': cohort['description'], 'query': cohort['query']},
                  'cases': {'selected': [gid for gid in order if gid in selected], 'companions': companions},
                  'variables': {**changes, 'rerun': rerun, 'batching': status},
                  'batching': {'status': status, 'formed': formed}, 'rerun': rerun,
                  'estimate': estimate(composed, prompts, groups, ledger, first, results, clients, args.price_multiplier)}
        save(temporary/'experiment.json', record)
        save(temporary/'executions.json', [])
    publish(folder, build)
    return read(folder/'experiment.json')


def load(folder, controlled=False):
    path = folder/'experiment.json'
    if not path.exists():
        raise ValueError(f'No experiment plan at {folder}')
    record = read(path)
    if record.get('schema_version') != PLAN_VERSION:
        raise ValueError('Unsupported experiment plan version; plan the experiment again')
    if controlled and record.get('kind') == 'retrospective':
        raise ValueError('A retrospective comparison is already finalized and has nothing to run')
    return record


# Running and finalizing -----------------------------------------------------
def execute(args):
    folder = args.root/args.name
    record, run_dir = load(folder, controlled=True), folder/'run'
    if (folder/'candidate').exists():
        raise ValueError('This experiment is finalized; plan a new experiment for further work')
    before = charges((run_dir/'calls').rglob('*.result.json'), inherited=False) if (run_dir/'calls').exists() else 0
    print(f'Estimated new spending up to ${record["estimate"]["upper_bound_usd"]:.4f}. '
          f'New spending so far ${before:.4f}; cap ${args.max_spend:.6g}.', flush=True)
    execution = {'started_at': datetime.now(timezone.utc).isoformat(), 'max_new_spend_usd': args.max_spend,
                 'price_multiplier': args.price_multiplier, 'workers': args.workers,
                 'retry_failed': args.retry_failed, 'retry_max_output_tokens': args.retry_max_output_tokens,
                 'first_pass_only': args.first_pass_only, 'code_revision': code_revision()}
    # Inherited calls do not count toward the cap, which covers new spending only.
    options = SimpleNamespace(root=folder, name='run', max_spend=args.max_spend,
                              price_multiplier=args.price_multiplier, workers=args.workers, max_chars=None,
                              first_pass_only=args.first_pass_only, api_base_url=args.api_base_url,
                              api_url=args.api_url, retry_failed=args.retry_failed,
                              retry_max_output_tokens=args.retry_max_output_tokens)
    try:
        appeals.run(options, inherit=Path(record['base']['run'])/'calls', rerun=record.get('rerun', []))
        execution['outcome'] = 'finished'
    except BaseException as exc:
        execution.update(outcome='stopped', error=str(exc) or type(exc).__name__)
        raise
    finally:
        after = charges((run_dir/'calls').rglob('*.result.json'), inherited=False)
        execution.update(finished_at=datetime.now(timezone.utc).isoformat(), new_spend_usd=after-before,
                         cumulative_new_spend_usd=after, status_counts=run_progress(run_dir)['statuses'])
        save(folder/'executions.json', [*read(folder/'executions.json'), execution])
        print(f'New spending this execution ${after-before:.4f}; experiment total ${after:.4f}.', flush=True)
    return execution


def finalize(args):
    folder = args.root/args.name
    record, run_dir = load(folder, controlled=True), folder/'run'
    for name in ('baseline', 'candidate', 'comparison.json'):
        if (folder/name).exists():
            raise ValueError(f'{name} already exists; finalized artifacts are never replaced')
    executions = read(folder/'executions.json')
    if not executions:
        raise ValueError('Run the experiment before finalizing it')
    states = [read(p) for p in (run_dir/'calls').rglob('*.result.json')]
    new = [s for s in states if not s.get('inherited_from')]
    new_cost = sum(s.get('charged_or_reserved_usd') or 0 for s in new)
    inherited = sum(s.get('charged_or_reserved_usd') or 0 for s in states if s.get('inherited_from'))
    cost = {'kind': 'experiment', 'inherited_usd': inherited, 'new_usd': new_cost, 'recorded_usd': inherited+new_cost,
            'reserved_usd': sum(s.get('charged_or_reserved_usd') or 0 for s in new if s.get('status') == 'pending'),
            'estimated_usd': record['estimate']['upper_bound_usd'],
            'completeness': 'reported' if all(s.get('usage') or s.get('status') == 'ok' or
                                              s.get('charged_or_reserved_usd') == 0 for s in new) else 'partial'}
    revision = code_revision()
    cases = record['cases']['selected']+record['cases']['companions']
    summary = {k: record[k] for k in ('name', 'hypothesis', 'cohort', 'variables')}
    base_calls = Path(record['base']['run'])/'calls'
    create_experiment(record['base']['run'], folder/'baseline', name=args.name+'-baseline',
                      code_revision=revision['commit'], case_ids=cases,
                      cost={'kind': 'inherited', 'recorded_usd': case_charges(base_calls, cases),
                            'scope': 'Base run calls for the selected cases, including retried attempts',
                            'run_recorded_usd': charges(base_calls.rglob('*.result.json')), 'completeness': 'reported'})
    create_experiment(run_dir, folder/'candidate', name=args.name+'-candidate', code_revision=revision['commit'],
                      kind='experiment_candidate', cost=cost, experiment=summary,
                      execution={'performed': True, 'new_model_calls': len(new), 'new_model_cost_usd': new_cost,
                                 'executions': len(executions)})
    base_batches = {frozenset(m) for _, m in review_batches(base_calls)}
    run_batches = {frozenset(m) for _, m in review_batches(run_dir/'calls', recursive=True)}
    observed = {'planned': record['batching']['status'],
                'observed': 'none_sent' if not run_batches else 'preserved' if run_batches <= base_batches else 'changed',
                'batches': sorted(sorted(b) for b in run_batches)}
    return write_comparison(folder, record, cost, observed, revision)


def write_comparison(folder, record, cost, batching_state, revision):
    """Summarize finalized baseline and candidate artifacts in comparison.json."""
    baseline, candidate = read_experiment(folder/'baseline'), read_experiment(folder/'candidate')
    roles = {gid: 'companion' for gid in record['cases']['companions']}
    result = impact(baseline['canonical'], candidate['canonical'], evaluate(baseline, candidate), roles=roles)
    rows = [{'case_id': c['case_id'], 'role': c['role'], 'verdict': c['verdict'], 'changes': c['chips'],
             'baseline_status': c['processing']['baseline'], 'candidate_status': c['processing']['candidate'],
             'baseline_records': c['records']['baseline'], 'candidate_records': c['records']['candidate']}
            for c in result['cases']]
    comparison = {'schema_version': 'appeal-run-comparison-1', 'created_at': datetime.now(timezone.utc).isoformat(),
                  'experiment': record['name'], 'reference': None, 'code_revision': revision,
                  'batching': batching_state,
                  'impact': {role: {k: group[k] for k in ('cases', 'verdicts', 'flows', 'headline', 'stability', 'records')}
                             for role, group in result['groups'].items()},
                  'transitions': dict(Counter(f'{r["baseline_status"]} -> {r["candidate_status"]}' for r in rows)),
                  'records': {'baseline': sum(r['baseline_records'] for r in rows),
                              'candidate': sum(r['candidate_records'] for r in rows)},
                  'cost': cost, 'cases': rows}
    save(folder/'comparison.json', comparison)
    return comparison


def retrospective(args):
    """Record two existing runs of the same frozen cases as an experiment; no model calls."""
    folder = new_folder(args.root, args.name)
    runs = {side: path.resolve() for side, path in (('baseline', args.base_run), ('candidate', args.candidate_run))}
    loaded = {side: load_run(path) for side, path in runs.items()}
    manifests = {side: value[0] for side, value in loaded.items()}
    hashes = {side: {g['group_id']: g['source_sha256'] for g in m['groups']} for side, m in manifests.items()}
    shared = [gid for gid in hashes['baseline'] if hashes['candidate'].get(gid) == hashes['baseline'][gid]]
    if args.cohort:
        cohort = read_cohort(args.cohort)
        selected = [c['case_id'] for c in cohort['cases']]
        absent = [gid for gid in selected if gid not in shared]
        if absent:
            raise ValueError('Cohort cases absent from either run or with different sources: '+', '.join(absent))
        cohort_record = {'path': str(args.cohort.resolve()), 'sha256': cohort['sha256'], 'name': cohort['name'],
                         'description': cohort['description'], 'query': cohort['query']}
    else:
        selected = shared
        cohort_record = {'path': None, 'sha256': None, 'name': 'shared cases',
                         'description': 'Every case both runs share with an identical frozen source',
                         'query': {'shared_sources': True}}
    if not selected:
        raise ValueError('The runs share no cases with identical frozen sources')
    (_, base_prompts, base_recipe), (_, prompts, recipe) = loaded['baseline'], loaded['candidate']
    revision = code_revision()
    record = {'schema_version': PLAN_VERSION, 'kind': 'retrospective', 'name': args.name,
              'created_at': datetime.now(timezone.utc).isoformat(), 'hypothesis': args.hypothesis,
              'code_revision': revision,
              'base': {'run': str(runs['baseline']), 'name': manifests['baseline']['name'],
                       'manifest_sha256': digest((runs['baseline']/'manifest.json').read_text()),
                       'recipe_sha256': recipes.sha256(base_recipe)},
              'candidate_run': {'run': str(runs['candidate']), 'name': manifests['candidate']['name'],
                                'manifest_sha256': digest((runs['candidate']/'manifest.json').read_text()),
                                'recipe_sha256': recipes.sha256(recipe)},
              'cohort': cohort_record, 'cases': {'selected': selected, 'companions': []},
              'variables': {**variables(base_recipe, base_prompts, recipe, prompts), 'rerun': [],
                            'batching': 'not_controlled'},
              'batching': {'status': 'not_controlled'}, 'rerun': [], 'estimate': None}
    costs = {side: {'kind': 'inherited', 'recorded_usd': case_charges(path/'calls', selected),
                    'scope': 'Recorded calls for the selected cases, including retried attempts',
                    'run_recorded_usd': charges((path/'calls').rglob('*.result.json')), 'completeness': 'reported'}
             for side, path in runs.items()}

    def build(temporary):
        create_experiment(runs['baseline'], temporary/'baseline', name=args.name+'-baseline',
                          code_revision=revision['commit'], case_ids=selected, cost=costs['baseline'])
        create_experiment(runs['candidate'], temporary/'candidate', name=args.name+'-candidate',
                          code_revision=revision['commit'], case_ids=selected, cost=costs['candidate'],
                          kind='experiment_candidate',
                          experiment={k: record[k] for k in ('name', 'hypothesis', 'cohort', 'variables')},
                          execution={'performed': False, 'new_model_calls': 0, 'new_model_cost_usd': 0,
                                     'retrospective': True})
        save(temporary/'experiment.json', record)
        save(temporary/'executions.json', [])
        write_comparison(temporary, record, costs['candidate'], {'planned': 'not_controlled', 'observed': None}, revision)
    publish(folder, build)
    return read(folder/'comparison.json')


def describe(folder):
    record = load(folder)
    if record.get('kind') == 'retrospective':
        return {k: record[k] for k in ('name', 'kind', 'hypothesis', 'variables')} | {
            'base': record['base']['name'], 'candidate': record['candidate_run']['name'],
            'cases': len(record['cases']['selected']), 'impact': read(folder/'comparison.json')['impact']}
    run_dir = folder/'run'
    spent = charges((run_dir/'calls').rglob('*.result.json'), inherited=False) if (run_dir/'calls').exists() else 0
    return {'name': record['name'], 'hypothesis': record['hypothesis'], 'base': record['base']['name'],
            'cohort': record['cohort']['name'], 'cases': {k: len(v) for k, v in record['cases'].items()},
            'variables': record['variables'], 'rerun': record['rerun'], 'batching': record['batching']['status'],
            'estimate': {k: v for k, v in record['estimate'].items() if k != 'notes'},
            'executions': len(read(folder/'executions.json')), 'new_spend_usd': spent,
            'status_counts': run_progress(run_dir)['statuses'], 'finalized': (folder/'comparison.json').exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=EXPERIMENTS, help='Experiment runs (default: data/experiments)')
    parser.add_argument('--definitions', type=Path, default=DEFINITIONS, help='Experiment definitions (default: experiments)')
    sub = parser.add_subparsers(dest='command', required=True)
    starting = sub.add_parser('init', help='Create a definition directory to fill in')
    starting.add_argument('name')
    starting.add_argument('--base-run', type=Path, required=True)
    starting.add_argument('--hypothesis', required=True)
    planning = sub.add_parser('plan', help='Compose and freeze a definition; no credentials or model calls')
    planning.add_argument('name')
    planning.add_argument('--price-multiplier', type=float, default=1, help='For the estimate; use 2 for peak pricing')
    status = sub.add_parser('status', help='Summarize a plan, its executions and progress')
    status.add_argument('name')
    running = sub.add_parser('run', help='Execute or resume; makes paid model calls')
    running.add_argument('name')
    running.add_argument('--max-spend', type=float, required=True, help='Cumulative cap on new spending for this experiment, in USD')
    running.add_argument('--price-multiplier', type=float, default=1)
    running.add_argument('--workers', type=int, default=3)
    running.add_argument('--first-pass-only', action='store_true')
    running.add_argument('--retry-failed', action='store_true')
    running.add_argument('--retry-max-output-tokens', type=int)
    endpoint = running.add_mutually_exclusive_group()
    endpoint.add_argument('--api-base-url')
    endpoint.add_argument('--api-url')
    finishing = sub.add_parser('finalize', help='Publish baseline/candidate artifacts and a comparison')
    finishing.add_argument('name')
    comparing = sub.add_parser('compare', help='Record two existing runs as a retrospective experiment; no model calls')
    comparing.add_argument('name')
    comparing.add_argument('--base-run', type=Path, required=True)
    comparing.add_argument('--candidate-run', type=Path, required=True)
    comparing.add_argument('--cohort', type=Path, help='Default: every case both runs share with identical sources')
    comparing.add_argument('--hypothesis', required=True, help='What differs between the runs and what it should change')
    args = parser.parse_args()
    if not appeals.SAFE.fullmatch(args.name):
        parser.error('Invalid experiment name')
    try:
        if args.command == 'init':
            folder = init(args)
            print(f'Created {folder}. Add a cohort, then any recipe.json, prompts/ or components/ overrides.')
        elif args.command == 'plan':
            if args.price_multiplier <= 0:
                parser.error('Positive price multiplier required')
            plan(args)
            print(json.dumps(describe(args.root/args.name), indent=2, ensure_ascii=False))
            print(f'Planned {args.root/args.name}. No model calls were made.')
        elif args.command == 'status':
            print(json.dumps(describe(args.root/args.name), indent=2, ensure_ascii=False))
        elif args.command == 'run':
            if args.max_spend <= 0 or args.price_multiplier <= 0 or args.workers < 1:
                parser.error('Positive budget, multiplier and worker count required')
            if args.retry_max_output_tokens is not None and (not args.retry_failed or args.retry_max_output_tokens < 1):
                parser.error('Positive --retry-max-output-tokens requires --retry-failed')
            execute(args)
        elif args.command == 'compare':
            comparison = retrospective(args)
            print(json.dumps({k: comparison[k] for k in ('impact', 'records', 'cost')}, indent=2))
            print('Open it in the experiment dashboard: make experiments-view')
        else:
            comparison = finalize(args)
            print(json.dumps({k: comparison[k] for k in ('impact', 'transitions', 'records', 'batching', 'cost')}, indent=2))
            print('Open it in the experiment dashboard: make experiments-view')
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
