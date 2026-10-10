"""Plan, run and finalize targeted extraction experiments on frozen cases.

An experiment is a new appeals-1 run over a frozen cohort from a base run, with
explicit changes to prompts or settings. ``plan`` copies frozen sources and
reusable saved work and estimates spending; it reads no credentials and makes
no model calls. ``run`` executes the existing pipeline under a cap on new
spending. ``finalize`` publishes cohort-restricted baseline and candidate
artifacts with an unscored comparison. The base run is only read.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import appeals
import group_scout as gs
from appeal_assembly import run_progress
from appeal_experiments import create_experiment, read_experiment
from appeal_cohorts import read_cohort
from appeal_evaluation import evaluate
from appeal_impact import impact
from appeal_runtime import digest, read, save
from config import ROOT

PLAN_VERSION = 'appeal-run-experiment-1'
EXPERIMENTS = ROOT/'data/experiments'
STAGES = ('triage', 'extract', 'review', 'duration')
# Prompt files each stage's requests contain (see appeal_assembly.execution_recipe).
STAGE_PROMPTS = {'triage': {'system', 'triage'}, 'extract': {'system', 'triage', 'extract'},
                 'review': {'primer', 'extract', 'triage', 'review'},
                 'duration': {'primer', 'extract', 'triage', 'review', 'duration'}}
# Request settings and the stages whose requests carry them.
SETTINGS = ('model', 'reasoning', 'max_output_tokens', 'review_max_output_tokens')
STAGE_SETTINGS = {'triage': {'model', 'reasoning', 'max_output_tokens'},
                  'extract': {'model', 'reasoning', 'max_output_tokens'},
                  'review': {'model', 'reasoning', 'review_max_output_tokens'},
                  'duration': {'model', 'reasoning', 'review_max_output_tokens'}}
REASONING = ('low', 'medium', 'high')
REVIEW_CALL = re.compile(r'review-[0-9a-f]{16}\Z')


def code_revision():
    def git(*command):
        return subprocess.run(['git', *command], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    try:
        return {'commit': git('rev-parse', 'HEAD'),
                'dirty': bool(git('status', '--porcelain', '--untracked-files=no'))}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': None}


def charges(paths):
    return sum(read(p).get('charged_or_reserved_usd') or 0 for p in paths)


def call_files(calls, name):
    """A saved call's files, including any JSON-repair subcall; attempts excluded."""
    return sorted(p for p in calls.iterdir() if p.is_file() and p.name.startswith(name+'.'))


def call_ok(calls, name):
    path = calls/f'{name}.result.json'
    return path.exists() and read(path).get('status') == 'ok'


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


def run_settings(manifest):
    """A run's request settings; review inherits the first-pass output allowance."""
    value = {'model': manifest.get('model'), 'reasoning': manifest.get('reasoning', 'high'),
             'max_output_tokens': manifest.get('max_output_tokens', 65536)}
    return {**value, 'review_max_output_tokens': manifest.get('review_max_output_tokens', value['max_output_tokens'])}


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


def triage_request(settings, prompts, source):
    request = gs.triage_payload(settings['model'], prompts, source, settings['reasoning'])
    request['max_tokens'] = settings['max_output_tokens']
    return request


def bound_usd(known_bytes, messages, unknown_tokens, max_tokens, multiplier):
    """Mirror request_cost_bound; unknown model-written content counts as max_tokens."""
    tokens = known_bytes + 256*messages + 1024 + unknown_tokens
    return multiplier*(tokens*gs.PRICES_PER_M['cache_miss'] + max_tokens*gs.PRICES_PER_M['output'])/1_000_000


def plan(args):
    folder = new_folder(args.root, args.name)
    base = args.base_run.resolve()
    base_manifest, base_prompts = read(base/'manifest.json'), read(base/'prompts.json')
    if base_manifest.get('schema_version') != 'appeals-1':
        raise ValueError('The base run must be an appeals-1 run')
    if digest(json.dumps(base_prompts, sort_keys=True)) != base_manifest['prompt_sha256']:
        raise ValueError('Base run prompts changed')
    cohort = read_cohort(args.cohort)
    base_groups = {g['group_id']: g for g in base_manifest['groups']}
    for case in cohort['cases']:
        group = base_groups.get(case['case_id'])
        if group is None or group['source_sha256'] != case['source_sha256']:
            raise ValueError(f'Cohort case {case["case_id"]} is absent from the base run or has a different source')

    # Review batches pair groups; a pair's companion supplies the same request context.
    calls = base/'calls'
    batches = dict(review_batches(calls))
    membership = {gid: members for members in batches.values() for gid in members}
    selected = [case['case_id'] for case in cohort['cases']]
    wanted = set(selected)
    if not args.no_companions:
        wanted |= {gid for s in selected for gid in membership.get(s, [])}
    order = [g['group_id'] for g in base_manifest['groups'] if g['group_id'] in wanted]
    companions = [gid for gid in order if gid not in selected]
    sources = {}
    for gid in order:
        text = (base/'sources'/f'{gid}.txt').read_text()
        if digest(text) != base_groups[gid]['source_sha256']:
            raise ValueError('Base frozen source changed: '+gid)
        sources[gid] = text

    prompts, prompt_changes = dict(base_prompts), {}
    for spec in args.prompt or []:
        key, _, path = spec.partition('=')
        if key not in prompts or not path:
            raise ValueError(f'Use --prompt NAME=FILE with NAME one of: {", ".join(sorted(prompts))}')
        text = Path(path).read_text()
        if text != prompts[key]:
            prompts[key] = text
            prompt_changes[key] = {'base_sha256': digest(base_prompts[key]), 'sha256': digest(text),
                                   'file': str(Path(path).resolve())}
    base_settings = run_settings(base_manifest)
    requested = {'model': args.model, 'reasoning': args.reasoning, 'max_output_tokens': args.max_output_tokens,
                 'review_max_output_tokens': args.review_max_output_tokens}
    settings = {k: requested[k] if requested[k] is not None else base_settings[k] for k in SETTINGS}
    if args.max_output_tokens is not None and args.review_max_output_tokens is None:
        settings['review_max_output_tokens'] = settings['max_output_tokens']
    setting_changes = {k: {'base': base_settings[k], 'value': settings[k]} for k in SETTINGS if settings[k] != base_settings[k]}

    reuse, upstream = {}, True
    for stage in STAGES:
        unchanged = not (STAGE_SETTINGS[stage] & set(setting_changes)) and not (STAGE_PROMPTS[stage] & set(prompt_changes))
        forced = args.rerun_from is not None and STAGES.index(stage) >= STAGES.index(args.rerun_from)
        upstream = reuse[stage] = upstream and unchanged and not forced

    copy_calls, first_pass, mismatched = [], {}, []
    for gid in order:
        triage_name = f'{gid}.triage'
        if reuse['triage'] and call_ok(calls, triage_name):
            # Reuse only a byte-identical request; anything else is a new call.
            if read(calls/f'{triage_name}.request.json') == triage_request(settings, prompts, sources[gid]):
                copy_calls.append(triage_name)
            else:
                mismatched.append(triage_name)
        if reuse['extract'] and triage_name in copy_calls:
            checkpoint = base/'first_pass'/f'{gid}.json'
            if checkpoint.exists() and read(checkpoint)['status'] in ('extracted', 'complete'):
                first_pass[gid] = read(checkpoint)
                if call_ok(calls, f'{gid}.extract'):
                    copy_calls.append(f'{gid}.extract')
    if mismatched:
        raise ValueError('Saved triage requests differ although no triage input changed: '+', '.join(mismatched))

    # Predict review batches when every first pass is reused; otherwise verify after running.
    review_chars = base_manifest['review_batch_chars']
    if all(gid in first_pass or not base_groups[gid]['eligible'] for gid in order):
        retained = [{'group': gid, 'source': sources[gid], 'candidate': first_pass[gid]['candidate']}
                    for gid in order if gid in first_pass and first_pass[gid]['status'] == 'extracted'
                    and first_pass[gid].get('candidate') is not None]
        predicted = [[c['group'] for c in batch] for batch in appeals.packs(retained, 2, review_chars)]
        changed = [batch for batch in predicted if any(set(membership.get(g, [])) != set(batch) for g in batch)]
        batching = {'status': 'changed' if changed else 'preserved', 'predicted': predicted, 'changed': changed}
    else:
        predicted, batching = None, {'status': 'verify_after_first_pass'}
    if args.no_companions:
        batching['status'] = 'changed'
    if batching['status'] == 'changed' and not args.allow_batch_change:
        raise ValueError('Review batch membership would differ from the base run; '
                         'pass --allow-batch-change to record batching as a changed variable')
    if reuse['review'] and predicted is not None:
        for batch in predicted:
            name = 'review-'+digest('|'.join(batch))[:16]
            if call_ok(calls, name):
                copy_calls.append(name)
                if reuse['duration']:
                    for path in sorted(calls.glob(f'duration-{name[7:]}-*.request.json')):
                        if call_ok(calls, path.name[:-len('.request.json')]):
                            copy_calls.append(path.name[:-len('.request.json')])

    # Historical spending on the same work, and a conservative bound for new calls.
    max_tokens, multiplier = settings['max_output_tokens'], args.price_multiplier
    review_tokens = settings['review_max_output_tokens']
    eligible = [gid for gid in order if base_groups[gid]['eligible']]
    copied = set(copy_calls)
    counts, upper, historical = Counter(), 0.0, 0.0
    for gid in eligible:
        triage = triage_request(settings, prompts, sources[gid])
        triage_bytes = sum(len(m['content'].encode()) for m in triage['messages'])
        if f'{gid}.triage' not in copied:
            counts['triage'] += 1
            upper += bound_usd(triage_bytes, 2, 0, max_tokens, multiplier)
        if gid not in first_pass:
            counts['extract'] += 1  # Upper bound: assume every group routes to extraction.
            upper += bound_usd(triage_bytes+len(prompts['extract'].encode()), 4, max_tokens, max_tokens, multiplier)
        for stage in ('triage', 'extract'):
            if f'{gid}.{stage}' not in copied:
                historical += charges(calls.glob(f'{gid}.{stage}.*result.json'))
    prefix = sum(len(prompts[k].encode()) for k in ('primer', 'extract', 'triage', 'review')) + 2048
    estimated_batches = predicted if predicted is not None else [[gid] for gid in eligible]
    for batch in estimated_batches:
        known, unknown = prefix + sum(len(sources[g].encode()) for g in batch), 0
        for g in batch:
            if g in first_pass:
                known += sum(len(json.dumps(first_pass[g].get(k), ensure_ascii=False).encode())
                             for k in ('triage', 'candidate'))
            else:
                unknown += 2*max_tokens  # Triage and extraction output in the review request.
        name = 'review-'+digest('|'.join(batch))[:16]
        if name not in copied:
            counts['review'] += 1
            upper += bound_usd(known, 2, unknown, review_tokens, multiplier)
        if not any(c.startswith(f'duration-{name[7:]}-') for c in copied):
            # The follow-up repeats the review request, its answer and corrected records.
            counts['duration'] += 1
            upper += bound_usd(known+len(prompts['duration'].encode()), 4, unknown+2*review_tokens, review_tokens, multiplier)
    for name, members in batches.items():
        if set(members) & set(order):
            if name not in copied:
                historical += charges(calls.glob(f'{name}.*result.json'))
            historical += charges(p for p in calls.glob(f'duration-{name[7:]}-*result.json')
                                  if p.name[:-len('.result.json')].split('.')[0] not in copied)
    estimate = {'new_calls': dict(counts), 'upper_bound_usd': round(upper, 6),
                'historical_usd': round(historical, 6), 'price_multiplier': multiplier,
                'notes': ['The upper bound assumes every uncached response uses the full output allowance '
                          'and every eligible group needs extraction. It excludes optional JSON-repair calls '
                          'and retries, which the spending cap still governs.',
                          'Historical spending is what the base run recorded for the same groups and stages.']}

    record = {'schema_version': PLAN_VERSION, 'name': args.name,
              'created_at': datetime.now(timezone.utc).isoformat(),
              'hypothesis': args.hypothesis, 'code_revision': code_revision(),
              'base': {'run': str(base), 'name': base_manifest['name'],
                       'manifest_sha256': digest((base/'manifest.json').read_text()),
                       'prompt_sha256': base_manifest['prompt_sha256']},
              'cohort': {'path': str(args.cohort.resolve()), 'sha256': cohort['sha256'], 'name': cohort['name'],
                         'description': cohort['description'], 'query': cohort['query']},
              'cases': {'selected': [gid for gid in order if gid in selected], 'companions': companions},
              'variables': {'prompts': prompt_changes, 'settings': setting_changes, 'rerun_from': args.rerun_from,
                            'batching': batching['status'], 'companions_included': not args.no_companions},
              'batching': batching, 'reuse': {'stages': reuse}, 'estimate': estimate}

    def build(temporary):
        run_dir = temporary/'run'
        (run_dir/'calls').mkdir(parents=True)
        (run_dir/'.lock').touch()
        for gid in order:
            (run_dir/'sources').mkdir(exist_ok=True)
            (run_dir/'sources'/f'{gid}.txt').write_text(sources[gid])
        for gid, checkpoint in first_pass.items():
            save(run_dir/'first_pass'/f'{gid}.json', checkpoint)
        copied_files = [p for name in copy_calls for p in call_files(calls, name)]
        for path in copied_files:
            shutil.copy2(path, run_dir/'calls'/path.name)
        record['reuse'].update(calls=copy_calls, first_pass=sorted(first_pass),
                               inherited_usd=charges(p for p in copied_files if p.name.endswith('.result.json')))
        save(run_dir/'prompts.json', prompts)
        save(run_dir/'manifest.json', {
            'schema_version': 'appeals-1', 'name': args.name, 'created_at': record['created_at'],
            **{k: v for k, v in settings.items() if k != 'review_max_output_tokens'},
            # Recorded only when review and duration differ from the first pass.
            **({'review_max_output_tokens': settings['review_max_output_tokens']}
               if settings['review_max_output_tokens'] != settings['max_output_tokens'] else {}),
            'max_chars': base_manifest['max_chars'], 'review_batch_size': 2,
            'review_batch_chars': review_chars, 'prompt_sha256': digest(json.dumps(prompts, sort_keys=True)),
            # Inherited eligibility keeps the base run's cap history.
            **({'cap_changes': base_manifest['cap_changes']} if 'cap_changes' in base_manifest else {}),
            'groups': [base_groups[gid] for gid in order]})
        save(temporary/'experiment.json', record)
        save(temporary/'executions.json', [])
    publish(folder, build)
    return record


def load(folder, controlled=False):
    path = folder/'experiment.json'
    if not path.exists():
        raise ValueError(f'No experiment plan at {folder}')
    record = read(path)
    if record.get('schema_version') != PLAN_VERSION:
        raise ValueError('Unsupported experiment plan version')
    if controlled and record.get('kind') == 'retrospective':
        raise ValueError('A retrospective comparison is already finalized and has nothing to run')
    return record


def spent(run_dir):
    return run_progress(run_dir)['charged_usd']


def statuses(run_dir):
    return run_progress(run_dir)['statuses']


def execute(args):
    folder = args.root/args.name
    record, run_dir = load(folder, controlled=True), folder/'run'
    if (folder/'candidate').exists():
        raise ValueError('This experiment is finalized; plan a new experiment for further work')
    inherited = record['reuse']['inherited_usd']
    before = spent(run_dir)
    print(f'Estimated new spending: historical ${record["estimate"]["historical_usd"]:.4f}, '
          f'upper bound ${record["estimate"]["upper_bound_usd"]:.4f}. '
          f'New spending so far ${before-inherited:.4f}; cap ${args.max_spend:.6g}.', flush=True)
    execution = {'started_at': datetime.now(timezone.utc).isoformat(), 'max_new_spend_usd': args.max_spend,
                 'price_multiplier': args.price_multiplier, 'workers': args.workers,
                 'retry_failed': args.retry_failed, 'retry_max_output_tokens': args.retry_max_output_tokens,
                 'first_pass_only': args.first_pass_only, 'code_revision': code_revision()}
    # The pipeline cap covers every saved call, so inherited charges are added to it.
    options = SimpleNamespace(root=folder, name='run', max_spend=inherited+args.max_spend,
                              price_multiplier=args.price_multiplier, workers=args.workers, max_chars=None,
                              first_pass_only=args.first_pass_only, api_base_url=args.api_base_url,
                              api_url=args.api_url, retry_failed=args.retry_failed,
                              retry_max_output_tokens=args.retry_max_output_tokens)
    try:
        appeals.run(options)
        execution['outcome'] = 'finished'
    except BaseException as exc:
        execution.update(outcome='stopped', error=str(exc) or type(exc).__name__)
        raise
    finally:
        after = spent(run_dir)
        execution.update(finished_at=datetime.now(timezone.utc).isoformat(),
                         new_spend_usd=after-before, cumulative_new_spend_usd=after-inherited,
                         status_counts=statuses(run_dir))
        save(folder/'executions.json', [*read(folder/'executions.json'), execution])
        print(f'New spending this execution ${after-before:.4f}; experiment total ${after-inherited:.4f}.', flush=True)
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
    copied = set(record['reuse']['calls'])
    results = list((run_dir/'calls').rglob('*.result.json'))
    def inherited(path):
        name = path.name[:-len('.result.json')]
        return path.parent == run_dir/'calls' and any(name == c or name.startswith(c+'.') for c in copied)
    new = [p for p in results if not inherited(p)]
    states = [read(p) for p in new]
    new_cost = sum(s.get('charged_or_reserved_usd') or 0 for s in states)
    cost = {'kind': 'experiment', 'inherited_usd': record['reuse']['inherited_usd'], 'new_usd': new_cost,
            'recorded_usd': record['reuse']['inherited_usd']+new_cost,
            'reserved_usd': sum(s.get('charged_or_reserved_usd') or 0 for s in states if s.get('status') == 'pending'),
            'estimated_usd': record['estimate']['upper_bound_usd'],
            'completeness': 'reported' if all(s.get('usage') or s.get('status') == 'ok' or
                                              s.get('charged_or_reserved_usd') == 0 for s in states) else 'partial'}
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
    base_batches = {frozenset(m) for _, m in review_batches(Path(record['base']['run'])/'calls')}
    run_batches = {frozenset(m) for _, m in review_batches(run_dir/'calls', recursive=True)}
    batching = {'planned': record['batching']['status'],
                'observed': 'none_sent' if not run_batches else 'preserved' if run_batches <= base_batches else 'changed',
                'batches': sorted(sorted(b) for b in run_batches)}
    return write_comparison(folder, record, cost, batching, revision)


def write_comparison(folder, record, cost, batching, revision):
    """Summarize finalized baseline and candidate artifacts in comparison.json."""
    baseline, candidate = read_experiment(folder/'baseline'), read_experiment(folder/'candidate')
    roles = {gid: 'companion' for gid in record['cases']['companions']}
    result = impact(baseline['canonical'], candidate['canonical'], evaluate(baseline, candidate), roles=roles)
    rows = [{'case_id': c['case_id'], 'role': c['role'], 'verdict': c['verdict'], 'changes': c['chips'],
             'baseline_status': c['processing']['baseline'], 'candidate_status': c['processing']['candidate'],
             'baseline_records': c['records']['baseline'], 'candidate_records': c['records']['candidate']}
            for c in result['cases']]
    comparison = {'schema_version': 'appeal-run-comparison-1', 'created_at': datetime.now(timezone.utc).isoformat(),
                  'experiment': record['name'], 'reference': None, 'code_revision': revision, 'batching': batching,
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
    manifests = {side: read(path/'manifest.json') for side, path in runs.items()}
    prompts = {side: read(path/'prompts.json') for side, path in runs.items()}
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
    before, after = run_settings(manifests['baseline']), run_settings(manifests['candidate'])
    variables = {'prompts': {k: {'base_sha256': digest(prompts['baseline'][k]) if k in prompts['baseline'] else None,
                                 'sha256': digest(prompts['candidate'][k]) if k in prompts['candidate'] else None}
                             for k in sorted(set(prompts['baseline']) | set(prompts['candidate']))
                             if prompts['baseline'].get(k) != prompts['candidate'].get(k)},
                 'settings': {k: {'base': before[k], 'value': after[k]} for k in SETTINGS if before[k] != after[k]},
                 'rerun_from': None, 'batching': 'not_controlled', 'companions_included': False}
    revision = code_revision()
    record = {'schema_version': PLAN_VERSION, 'kind': 'retrospective', 'name': args.name,
              'created_at': datetime.now(timezone.utc).isoformat(), 'hypothesis': args.hypothesis,
              'code_revision': revision,
              'base': {'run': str(runs['baseline']), 'name': manifests['baseline']['name'],
                       'manifest_sha256': digest((runs['baseline']/'manifest.json').read_text()),
                       'prompt_sha256': manifests['baseline']['prompt_sha256']},
              'candidate_run': {'run': str(runs['candidate']), 'name': manifests['candidate']['name'],
                                'manifest_sha256': digest((runs['candidate']/'manifest.json').read_text()),
                                'prompt_sha256': manifests['candidate']['prompt_sha256']},
              'cohort': cohort_record, 'cases': {'selected': selected, 'companions': []},
              'variables': variables, 'batching': {'status': 'not_controlled'},
              'reuse': {'stages': {}, 'calls': [], 'inherited_usd': 0}, 'estimate': None}
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
    run_dir = folder/'run'
    executions = read(folder/'executions.json')
    inherited = record['reuse']['inherited_usd']
    if record.get('kind') == 'retrospective':
        return {k: record[k] for k in ('name', 'kind', 'hypothesis', 'variables')} | {
            'base': record['base']['name'], 'candidate': record['candidate_run']['name'],
            'cases': len(record['cases']['selected']), 'impact': read(folder/'comparison.json')['impact']}
    return {'name': record['name'], 'hypothesis': record['hypothesis'],
            'base': record['base']['name'], 'cohort': record['cohort']['name'],
            'cases': {k: len(v) for k, v in record['cases'].items()},
            'variables': record['variables'], 'reuse': {**record['reuse']['stages'],
            'calls': len(record['reuse']['calls']), 'inherited_usd': inherited},
            'estimate': {k: v for k, v in record['estimate'].items() if k != 'notes'},
            'executions': len(executions), 'new_spend_usd': spent(run_dir)-inherited,
            'status_counts': statuses(run_dir), 'finalized': (folder/'comparison.json').exists()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=EXPERIMENTS, help='Experiment directory (default: data/experiments)')
    sub = parser.add_subparsers(dest='command', required=True)
    planning = sub.add_parser('plan', help='Freeze an experiment; no credentials or model calls')
    planning.add_argument('name')
    planning.add_argument('--base-run', type=Path, required=True, help='appeals-1 run the cohort was selected from')
    planning.add_argument('--cohort', type=Path, required=True)
    planning.add_argument('--hypothesis', required=True, help='What the change is expected to do')
    planning.add_argument('--prompt', action='append', metavar='NAME=FILE', help='Replace one frozen prompt (repeatable)')
    planning.add_argument('--model')
    planning.add_argument('--reasoning', choices=REASONING)
    planning.add_argument('--max-output-tokens', type=int, help='Output allowance for every stage')
    planning.add_argument('--review-max-output-tokens', type=int, help='Output allowance for review and duration only')
    planning.add_argument('--rerun-from', choices=STAGES, help='Make new calls from this stage even if inputs are unchanged')
    planning.add_argument('--no-companions', action='store_true', help='Omit review-batch companions (changes batching)')
    planning.add_argument('--allow-batch-change', action='store_true', help='Record changed review batching instead of refusing')
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
        if args.command == 'plan':
            if any(v is not None and v < 1 for v in (args.max_output_tokens, args.review_max_output_tokens)) or args.price_multiplier <= 0:
                parser.error('Positive output allowance and price multiplier required')
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
