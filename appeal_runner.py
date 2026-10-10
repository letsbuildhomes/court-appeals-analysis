"""Plan, run and finalize experiments; name pipelines with tags.

An experiment definition lives in ``experiments/NAME/``:

    experiment.json   hypothesis, baseline (a tag or pipeline ID), cohort file and options
    experiment.py     build(base) returns the experiment's pipeline from the baseline's tree
    *.py              step classes the experiment adds, one per file
    prompts/*.md      prompts those steps load
    cohort.json       the frozen case selection (``appeal_cohorts.py select``)
    README.md         why, what changes, how to judge it

``plan`` builds the baseline and experiment trees, freezes the experiment's
pipeline, adds batch companions, and estimates new spending with a dry run; it
reads no credentials and makes no model calls. ``run`` executes the
experiment's run under a cap on new spending; identical calls, including the
production run's, are reused for free. ``finalize`` seals the run and compares
it with the baseline run on the same cases. ``compare`` records two existing
runs as a retrospective comparison.

``tag`` points a name such as ``production`` at a pipeline; ``tags`` shows each
tag's history. ``replay-production`` rebuilds the production run with the
ported legacy pipeline and its saved calls, with sending disabled, and checks
it against production's converted outputs.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import tempfile

from appeal_cohorts import read_cohort
from appeal_evaluation import evaluate
from appeal_impact import impact
from appeal_runtime import digest, read, save
from config import ROOT
from pipeline import providers, registry, runs
from pipeline.corpus import Corpus
from pipeline.steps import ModelStep
from pipeline.store import CallStore, build_index

PLAN_VERSION = 'appeal-experiment-3'
DEFINITION_VERSION = 'appeal-experiment-definition-2'
COMPARISON_VERSION = 'appeal-run-comparison-2'
EXPERIMENTS = ROOT/'data/experiments'
DEFINITIONS = ROOT/'experiments'
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
DEFINITION_KEYS = {'schema_version', 'hypothesis', 'baseline', 'cohort', 'companions', 'allow_batch_change',
                   'samples', 'notes'}
TEMPLATE = '''"""{hypothesis}

``build(base)`` receives the baseline pipeline's tree and returns this
experiment's tree. Trees are values: ``replace``, ``remove``, ``insert`` and
``update`` return new trees. Put each new step class in its own file beside
this one and import it relatively, e.g. ``from .strict_review import StrictReview``.
"""


def build(base):
    return base
'''


def new_folder(root, name):
    if not SAFE.fullmatch(name) or (root/name).exists():
        raise ValueError('Choose a new name using letters, numbers, dots, dashes or underscores')
    return root/name


def publish(folder, build):
    """Assemble a directory beside its destination, then publish it in one rename."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+folder.name+'-', dir=folder.parent))
    try:
        build(temporary)
        temporary.rename(folder)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


# Definitions ---------------------------------------------------------------
def init(args):
    """Scaffold a definition directory for a new experiment."""
    folder = new_folder(args.definitions, args.name)
    registry.resolve(args.baseline)
    folder.mkdir(parents=True)
    save(folder/'experiment.json', {'schema_version': DEFINITION_VERSION, 'hypothesis': args.hypothesis,
                                    'baseline': args.baseline, 'cohort': 'cohort.json', 'companions': True,
                                    'allow_batch_change': False, 'samples': {}})
    (folder/'experiment.py').write_text(TEMPLATE.format(hypothesis=args.hypothesis))
    (folder/'README.md').write_text(f'# {args.name}\n\n{args.hypothesis}\n\n'
                                    '## Why\n\n## What changes\n\n## How to judge it\n')
    return folder


def read_definition(folder):
    definition = read(Path(folder)/'experiment.json')
    if definition.get('schema_version') != DEFINITION_VERSION:
        raise ValueError('Unsupported experiment definition version')
    for key in ('hypothesis', 'baseline', 'cohort'):
        if not definition.get(key):
            raise ValueError(f'experiment.json needs {key}')
    unknown = set(definition) - DEFINITION_KEYS
    if unknown:
        raise ValueError('Unknown experiment.json keys: '+', '.join(sorted(unknown)))
    if not (Path(folder)/'experiment.py').exists():
        raise ValueError('The experiment needs experiment.py with build(base)')
    return definition


def definition_files(folder):
    """Every file in a definition directory with its hash, for provenance."""
    folder = Path(folder)
    return {str(p.relative_to(folder)): digest(p.read_text()) for p in sorted(folder.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


# Batches and estimates -----------------------------------------------------
def shared_groups(traces, cases=None):
    """Groups of two or more cases that shared a call, from traces."""
    found = {frozenset(e['members']) for trace in traces for e in trace or []
             if e.get('event') == 'call' and e.get('members')}
    return {g for g in found if cases is None or g <= set(cases)}


def companions(baseline, selected):
    """Cases that shared a call with a selected case in the baseline run."""
    loaded = runs.load(baseline, selected, traces=True)
    wanted = set(selected)
    for case in loaded['cases']:
        for entry in case['trace'] or []:
            if entry.get('event') == 'call' and entry.get('members'):
                wanted |= set(entry['members'])
    return [i for i in [c['case_id'] for c in runs.record(baseline)['cases']] if i in wanted and i not in selected]


def estimate(tree, outputs, calls, cases, multiplier):
    """New calls the dry run reached, plus conservative bounds for steps it could not reach."""
    walk = [path for path, _ in tree.walk()]
    models = [(path, step) for path, step in tree.walk() if isinstance(step, ModelStep)]
    counts, reached = Counter(), {}
    for case_id, (_, _, trace) in outputs.items():
        for entry in trace:
            if entry.get('event') == 'call' and entry.get('outcome') == 'dry_run':
                reached.setdefault(entry['step'], set()).add(entry['key'])
    for step, keys in reached.items():
        counts[step] = len(keys)
    upper, sizes = calls.would_spend(), {c.id: len(c.source.encode()) for c in cases}
    built = {}
    for case_id, (_, status, trace) in outputs.items():
        if status['state'] != 'in_progress':
            continue
        stopped = next((e['step'] for e in reversed(trace) if e.get('event') == 'pending'), None)
        if stopped is None:
            continue
        for path, step in models:
            if walk.index(path) <= walk.index(stopped):
                continue
            if step.model.name not in built:
                built[step.model.name] = providers.build(step.model.name, step.model.spec(), offline=True)
            tokens = step.model.settings.get('max_output_tokens', 0)
            size = sizes[case_id] + sum(len(p.text.encode()) for p in step.prompts.values())
            upper += built[step.model.name].estimate(size, 4, 2*tokens, tokens)*multiplier
            counts[path+' (unreached)'] += 1
    reused = {e['key']: e['usd'] for e in calls.entries if e['event'] == 'reused'}
    return {'new_calls': dict(counts), 'upper_bound_usd': round(upper, 6), 'reused_usd': round(sum(reused.values()), 6),
            'reused_calls': len(reused), 'price_multiplier': multiplier,
            'notes': ['The bound prices the calls the dry run reached at their exact requests, and later steps it '
                      'could not reach as if every unknown answer used its full output allowance, even for cases an '
                      'earlier step might exclude. It excludes JSON-repair calls and retries, which the spending cap '
                      'still governs.']}


# Planning ------------------------------------------------------------------
def plan(args):
    folder = new_folder(args.root, args.name)
    definition_dir = args.definitions/args.name
    definition = read_definition(definition_dir)
    baseline_id = registry.resolve(definition['baseline'])
    baseline_record = registry.load(baseline_id)
    base_tree = registry.build(baseline_record['source'], args.definitions)
    rebuilt_id, _ = registry.identify(base_tree)
    corpus = Corpus()
    if definition['cohort'] == 'all':
        cohort = {'name': 'all', 'description': 'Every corpus case', 'query': {'all': True}, 'sha256': None,
                  'cases': [{'case_id': i, 'source_sha256': corpus.entries[i]['source_sha256']} for i in corpus.ids()]}
        cohort_path = None
    else:
        cohort_path = (definition_dir/definition['cohort']).resolve()
        cohort = read_cohort(cohort_path)
    selected = [c['case_id'] for c in cohort['cases']]
    for case in cohort['cases']:
        entry = corpus.entries.get(case['case_id'])
        if entry is None or entry['source_sha256'] != case['source_sha256']:
            raise ValueError(f'Cohort case {case["case_id"]} is absent from the corpus or has a different source')
    baseline_run = runs.find(baseline_id, selected)
    keep = definition.get('companions', True)
    extra = companions(baseline_run, selected) if keep else []
    cases = [i for i in corpus.ids() if i in set(selected) | set(extra)]
    source = {'experiment': args.name, 'base': baseline_record['source']}
    tree = registry.build(source, args.definitions)
    candidate_id = registry.freeze(tree, source)
    samples = definition.get('samples', {})
    unknown = set(samples) - {p for p, _ in tree.walk()}
    if unknown:
        raise ValueError('samples names unknown steps: '+', '.join(sorted(unknown)))
    now = datetime.now(timezone.utc).isoformat()

    def build(temporary):
        meta = runs.create(temporary/'run', name=args.name, pipeline_id=candidate_id, case_ids=cases, corpus=corpus,
                           samples=samples)
        outputs, calls, loaded = runs.dry_run(meta, tree, CallStore(args.store), args.price_multiplier)
        traces = [t for _, _, t in outputs.values()]
        waiting = any(e.get('event') == 'pending' and e.get('reason', '').startswith('Waiting')
                      for t in traces for e in t)
        before = shared_groups([c['trace'] for c in runs.load(baseline_run, cases, traces=True)['cases']], cases)
        after = shared_groups(traces, cases)
        if not keep:
            status = 'changed'
        elif waiting:
            status = 'verify_after_run'
        else:
            status = 'preserved' if before == after else 'changed'
        if status == 'changed' and not definition.get('allow_batch_change'):
            raise ValueError('Batch membership would differ from the baseline run; keep companions, or set '
                             '"allow_batch_change": true to record batching as a changed variable')
        shutil.copytree(definition_dir, temporary/'definition', ignore=shutil.ignore_patterns('__pycache__'))
        record = {'schema_version': PLAN_VERSION, 'kind': 'experiment', 'name': args.name, 'created_at': now,
                  'hypothesis': definition['hypothesis'], 'code_revision': registry.git_commit(),
                  'definition': {'path': str(definition_dir), 'files': definition_files(definition_dir)},
                  'baseline': {'ref': definition['baseline'], 'pipeline': baseline_id, 'run': str(baseline_run),
                               'rebuilt': rebuilt_id},
                  'candidate': {'pipeline': candidate_id, 'run': str(folder/'run')},
                  'cohort': {'path': str(cohort_path) if cohort_path else None, 'sha256': cohort['sha256'],
                             'name': cohort['name'], 'description': cohort['description'], 'query': cohort['query']},
                  'cases': {'selected': [i for i in cases if i in set(selected)], 'companions': extra},
                  'samples': samples, 'batching': {'status': status, 'groups': sorted(sorted(g) for g in after)},
                  'estimate': estimate(tree, outputs, calls, loaded, args.price_multiplier)}
        save(temporary/'experiment.json', record)
    publish(folder, build)
    return load(folder)


def load(folder):
    path = Path(folder)/'experiment.json'
    if not path.exists():
        raise ValueError(f'No experiment at {folder}')
    record = read(path)
    if record.get('schema_version') != PLAN_VERSION:
        raise ValueError('Unsupported experiment record version')
    return record


# Running and finalizing ----------------------------------------------------
def execute(args):
    folder = args.root/args.name
    record = load(folder)
    if record['kind'] == 'retrospective':
        raise ValueError('A retrospective comparison has nothing to run')
    if (folder/'comparison.json').exists():
        raise ValueError('This experiment is finalized; plan a new experiment for further work')
    print(f'Estimated new spending up to ${record["estimate"]["upper_bound_usd"]:.4f}; cap ${args.max_spend:.6g}.',
          flush=True)
    execution = runs.run(folder/'run', store=CallStore(args.store), limit=args.max_spend,
                         multiplier=args.price_multiplier, workers=args.workers, retry_failed=args.retry_failed,
                         retry_max_tokens=args.retry_max_output_tokens,
                         endpoint={'base_url': args.api_base_url, 'api_url': args.api_url},
                         experiments=args.definitions)
    print(f'New spending this execution ${execution["new_spend_usd"]:.4f}; '
          f'experiment total ${execution["cumulative_new_spend_usd"]:.4f}. {execution["statuses"]}', flush=True)
    return execution


def finalize(args):
    folder = args.root/args.name
    record = load(folder)
    if record['kind'] == 'retrospective' or (folder/'comparison.json').exists():
        raise ValueError('Already finalized; comparisons are never replaced')
    run_dir = folder/'run'
    if not read(run_dir/'executions.json'):
        raise ValueError('Run the experiment before finalizing it')
    runs.seal(run_dir)
    cases = record['cases']['selected']+record['cases']['companions']
    traces = [c['trace'] for c in runs.load(run_dir, traces=True)['cases']]
    observed = shared_groups(traces, cases)
    planned = {frozenset(g) for g in record['batching']['groups']}
    batching = {'planned': record['batching']['status'],
                'observed': 'none_formed' if not observed else 'preserved' if observed == planned or
                record['batching']['status'] == 'preserved' and observed <= planned else 'changed',
                'groups': sorted(sorted(g) for g in observed)}
    return write_comparison(folder, record, Path(record['baseline']['run']), run_dir, cases,
                            {gid: 'companion' for gid in record['cases']['companions']}, batching,
                            runs.cost(run_dir, CallStore(args.store)))


def write_comparison(folder, record, baseline_dir, candidate_dir, cases, roles, batching, cost):
    baseline, candidate = runs.load(baseline_dir, cases), runs.load(candidate_dir, cases)
    result = impact(baseline, candidate, evaluate(baseline, candidate), roles=roles)
    rows = [{'case_id': c['case_id'], 'role': c['role'], 'verdict': c['verdict'], 'changes': c['chips'],
             'baseline_status': c['processing']['baseline'], 'candidate_status': c['processing']['candidate'],
             'baseline_records': c['records']['baseline'], 'candidate_records': c['records']['candidate']}
            for c in result['cases']]
    comparison = {'schema_version': COMPARISON_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'experiment': record['name'], 'reference': None, 'code_revision': registry.git_commit(),
                  'baseline': {'run': str(baseline_dir), 'pipeline': baseline['run']['pipeline']},
                  'candidate': {'run': str(candidate_dir), 'pipeline': candidate['run']['pipeline']},
                  'batching': batching,
                  'impact': {role: {k: group[k] for k in ('cases', 'verdicts', 'flows', 'headline', 'stability', 'records')}
                             for role, group in result['groups'].items()},
                  'transitions': dict(Counter(f'{r["baseline_status"]} -> {r["candidate_status"]}' for r in rows)),
                  'records': {'baseline': sum(r['baseline_records'] for r in rows),
                              'candidate': sum(r['candidate_records'] for r in rows)},
                  'cost': cost, 'cases': rows}
    save(folder/'comparison.json', comparison)
    return comparison


def retrospective(args):
    """Record two existing runs of the same frozen cases as a comparison; no model calls."""
    folder = new_folder(args.root, args.name)
    sides = {'baseline': args.base_run.resolve(), 'candidate': args.candidate_run.resolve()}
    metas = {side: runs.record(path) for side, path in sides.items()}
    hashes = {side: {c['case_id']: c['source_sha256'] for c in m['cases']} for side, m in metas.items()}
    shared = [i for i in hashes['baseline'] if hashes['candidate'].get(i) == hashes['baseline'][i]]
    if args.cohort:
        cohort = read_cohort(args.cohort)
        selected = [c['case_id'] for c in cohort['cases']]
        absent = [i for i in selected if i not in shared]
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
    record = {'schema_version': PLAN_VERSION, 'kind': 'retrospective', 'name': args.name,
              'created_at': datetime.now(timezone.utc).isoformat(), 'hypothesis': args.hypothesis,
              'code_revision': registry.git_commit(),
              'baseline': {'ref': None, 'pipeline': metas['baseline']['pipeline'], 'run': str(sides['baseline'])},
              'candidate': {'pipeline': metas['candidate']['pipeline'], 'run': str(sides['candidate'])},
              'cohort': cohort_record, 'cases': {'selected': selected, 'companions': []}, 'samples': {},
              'batching': {'status': 'not_controlled'}, 'estimate': None}

    def build(temporary):
        save(temporary/'experiment.json', record)
        cost = runs.cost(sides['candidate'], CallStore(args.store))
        write_comparison(temporary, record, sides['baseline'], sides['candidate'], selected, {},
                         {'planned': 'not_controlled', 'observed': None}, cost)
    publish(folder, build)
    return read(folder/'comparison.json')


def describe(folder):
    record = load(folder)
    summary = {k: record[k] for k in ('name', 'kind', 'hypothesis')}
    summary.update(baseline=record['baseline'], candidate=record['candidate'],
                   cases={k: len(v) for k, v in record['cases'].items()},
                   changes=registry.summary(registry.diff(registry.load(record['baseline']['pipeline'])['descriptor'],
                                                          registry.load(record['candidate']['pipeline'])['descriptor'])),
                   finalized=(Path(folder)/'comparison.json').exists())
    if record['kind'] == 'experiment':
        run_dir = Path(record['candidate']['run'])
        summary.update(samples=record['samples'], batching=record['batching']['status'],
                       estimate={k: v for k, v in record['estimate'].items() if k != 'notes'},
                       executions=len(read(run_dir/'executions.json')), statuses=dict(runs.progress(run_dir)['statuses']))
    if summary['finalized']:
        summary['impact'] = read(Path(folder)/'comparison.json')['impact']
    return summary


# Pipelines and tags --------------------------------------------------------
def tag(args):
    pipeline_id = registry.resolve(args.pipeline)
    evidence = {}
    if args.experiment:
        folder = args.root/args.experiment
        record = load(folder)
        if record['candidate']['pipeline'] != pipeline_id:
            raise ValueError(f'{args.experiment} tested {record["candidate"]["pipeline"]}, not {pipeline_id}')
        comparison = read(folder/'comparison.json')
        evidence = {'experiment': args.experiment, 'comparison': str(folder/'comparison.json'),
                    'headline': comparison['impact'].get('selected', {}).get('headline'),
                    'baseline': record['baseline']['pipeline']}
    return registry.set_tag(args.tag, pipeline_id, args.note, evidence)


def show_tags(args):
    log = registry.tag_log()
    chosen = [e for e in log if not args.tag or e['tag'] == args.tag]
    return {'current': registry.tags(), 'history': chosen}


def show_pipelines(args):
    current = {}
    for name, pipeline_id in registry.tags().items():
        current.setdefault(pipeline_id, []).append(name)
    return [{'id': p['id'], 'name': p['name'], 'created_at': p['created_at'], 'source': p['source'],
             'tags': current.get(p['id'], [])} for p in registry.stored()]


def show_diff(args):
    a, b = (registry.load(registry.resolve(ref))['descriptor'] for ref in (args.base, args.candidate))
    return registry.summary(registry.diff(a, b)) or ['No differences']


# Production and legacy runs ------------------------------------------------
def index_calls(args):
    provider = providers.build(args.model, {'model': args.model, 'env': 'APPEALS'}, offline=True)
    return build_index(args.store, args.name, args.run.resolve()/'calls', provider)


def import_legacy(args):
    from pipeline.legacy.convert import convert
    folder = new_folder(args.runs, args.name)
    return convert(args.run, folder, args.name)


def replay_production(args):
    """Rebuild production from its saved calls with sending disabled; check it matches."""
    from pipeline import legacy
    from pipeline.corpus import PRODUCTION
    from pipeline.legacy.convert import convert
    store_root = Path(args.store)
    if not (store_root/'index'/'production.json').exists():
        provider = providers.build('deepseek-flash', {'model': 'deepseek-flash', 'env': 'APPEALS'}, offline=True)
        build_index(store_root, 'production', PRODUCTION/'calls', provider)
    converted = args.runs/f'{args.name}-converted'
    if not converted.exists():
        convert(PRODUCTION, converted, f'{args.name}-converted')
    folder = new_folder(args.runs, args.name)
    source = {'module': 'pipeline.legacy', 'options': {}}
    tree = legacy.build()
    pipeline_id = registry.freeze(tree, source)
    corpus = Corpus(PRODUCTION)
    runs.create(folder, name=args.name, pipeline_id=pipeline_id, case_ids=corpus.ids(), corpus=corpus)
    runs.run(folder, tree=tree, store=CallStore(store_root), limit=0.0, workers=args.workers, offline=True)
    report = acceptance(folder, converted)
    save(folder/'acceptance.json', report)
    runs.seal(folder)
    return report


def acceptance(replayed, converted):
    """Compare a replay with converted outputs case by case."""
    a, b = runs.load(replayed), runs.load(converted)
    by_id = {c['case_id']: c for c in b['cases']}
    differences = []
    for case in a['cases']:
        other = by_id[case['case_id']]
        if case['status']['state'] != other['status']['state']:
            differences.append({'case_id': case['case_id'], 'difference': 'status',
                                'replayed': case['status']['state'], 'converted': other['status']['state'],
                                'errors': [e['error'] for e in case['status']['errors']][:2]})
        elif (case['disposition'], case['records']) != (other['disposition'], other['records']):
            differences.append({'case_id': case['case_id'], 'difference': 'result'})
    failed = [c['case_id'] for c in a['cases'] if c['status']['state'] == 'failed']
    return {'created_at': datetime.now(timezone.utc).isoformat(), 'replayed': str(replayed),
            'converted': str(converted), 'pipeline': a['run']['pipeline'], 'cases': len(a['cases']),
            'statuses': dict(Counter(c['status']['state'] for c in a['cases'])), 'failed': failed,
            'matches': not differences, 'differences': differences}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=EXPERIMENTS, help='Experiment records (default: data/experiments)')
    parser.add_argument('--definitions', type=Path, default=DEFINITIONS, help='Experiment definitions (default: experiments)')
    parser.add_argument('--runs', type=Path, default=runs.RUNS, help='Standalone runs (default: data/runs)')
    parser.add_argument('--store', type=Path, default=runs.STORE, help='Call store (default: data/calls)')
    sub = parser.add_subparsers(dest='command', required=True)
    starting = sub.add_parser('init', help='Create a definition directory to fill in')
    starting.add_argument('name')
    starting.add_argument('--hypothesis', required=True)
    starting.add_argument('--baseline', default='production', help='Tag or pipeline ID (default: production)')
    planning = sub.add_parser('plan', help='Build and freeze an experiment; no credentials or model calls')
    planning.add_argument('name')
    planning.add_argument('--price-multiplier', type=float, default=1, help='For the estimate; use 2 for peak pricing')
    status = sub.add_parser('status', help='Summarize an experiment')
    status.add_argument('name')
    running = sub.add_parser('run', help='Execute or resume; makes paid model calls')
    running.add_argument('name')
    running.add_argument('--max-spend', type=float, required=True, help='Cumulative cap on new spending, in USD')
    running.add_argument('--price-multiplier', type=float, default=1)
    running.add_argument('--workers', type=int, default=3)
    running.add_argument('--retry-failed', action='store_true')
    running.add_argument('--retry-max-output-tokens', type=int)
    endpoint = running.add_mutually_exclusive_group()
    endpoint.add_argument('--api-base-url')
    endpoint.add_argument('--api-url')
    finishing = sub.add_parser('finalize', help='Seal the run and compare it with the baseline')
    finishing.add_argument('name')
    comparing = sub.add_parser('compare', help='Record two existing runs as a retrospective comparison; no model calls')
    comparing.add_argument('name')
    comparing.add_argument('--base-run', type=Path, required=True)
    comparing.add_argument('--candidate-run', type=Path, required=True)
    comparing.add_argument('--cohort', type=Path, help='Default: every case both runs share with identical sources')
    comparing.add_argument('--hypothesis', required=True, help='What differs between the runs and what it should change')
    tagging = sub.add_parser('tag', help='Point a tag at a pipeline; the tag log keeps every earlier target')
    tagging.add_argument('tag')
    tagging.add_argument('pipeline', help='Pipeline ID or another tag')
    tagging.add_argument('--note', required=True, help='Why the tag moves')
    tagging.add_argument('--experiment', help='The finalized experiment that tested this pipeline')
    listing = sub.add_parser('tags', help='Current tags and their history')
    listing.add_argument('tag', nargs='?')
    sub.add_parser('pipelines', help='Stored pipelines and their tags')
    diffing = sub.add_parser('diff', help='What changed between two pipelines')
    diffing.add_argument('base')
    diffing.add_argument('candidate')
    indexing = sub.add_parser('index-calls', help='Index a legacy run\'s delivered calls for reuse')
    indexing.add_argument('run', type=Path)
    indexing.add_argument('--name', required=True)
    indexing.add_argument('--model', default='deepseek-flash')
    importing = sub.add_parser('import-legacy', help='Convert a legacy run\'s outputs into a run; no model calls')
    importing.add_argument('run', type=Path)
    importing.add_argument('--name', required=True)
    replaying = sub.add_parser('replay-production', help='Rebuild production from saved calls; no sending')
    replaying.add_argument('--name', default='production')
    replaying.add_argument('--workers', type=int, default=16)
    args = parser.parse_args()
    if getattr(args, 'name', None) is not None and not SAFE.fullmatch(args.name):
        parser.error('Invalid name')
    commands = {'init': init, 'plan': plan, 'status': lambda a: describe(a.root/a.name), 'run': execute,
                'finalize': finalize, 'compare': retrospective, 'tag': tag, 'tags': show_tags,
                'pipelines': show_pipelines, 'diff': show_diff, 'index-calls': index_calls,
                'import-legacy': import_legacy, 'replay-production': replay_production}
    try:
        if args.command == 'plan' and args.price_multiplier <= 0:
            parser.error('Positive price multiplier required')
        if args.command == 'run':
            if args.max_spend <= 0 or args.price_multiplier <= 0 or args.workers < 1:
                parser.error('Positive budget, multiplier and worker count required')
            if args.retry_max_output_tokens is not None and (not args.retry_failed or args.retry_max_output_tokens < 1):
                parser.error('Positive --retry-max-output-tokens requires --retry-failed')
        value = commands[args.command](args)
        if args.command == 'plan':
            value = describe(args.root/args.name)
        if args.command == 'init':
            print(f'Created {value}. Add a cohort and edit experiment.py.')
        elif args.command in ('finalize', 'compare'):
            print(json.dumps({k: value[k] for k in ('impact', 'transitions', 'records', 'batching', 'cost')}, indent=2))
            print('Open it in the experiment dashboard: make experiments-view')
        else:
            print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        if args.command == 'plan':
            print(f'Planned {args.root/args.name}. No model calls were made.')
    except (ValueError, KeyError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
