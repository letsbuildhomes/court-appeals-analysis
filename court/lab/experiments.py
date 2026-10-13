"""Experiments: a hypothesis, a candidate pipeline and its rounds of evidence.

An experiment is a directory ``experiments/NAME/``:

    experiment.json      name, hypothesis, the pinned baseline pipeline ID (and the tag it came from),
                         the candidate's definition name, the position runs start at, the input
                         datasets for that position, the focus (the position the keys apply to),
                         the probe keys, the confirm keys and an optional reference dataset
    plans/ROUND.json     the latest plan for a round: its cases and a dry-run cost estimate
    rounds/ROUND.json    one record per round run: the keys, both pipelines and runs, the definition
                         diff, which cases' inputs at the focus changed, the comparison summary and
                         every case's verdict, step-by-step data diffs, regressions and cost

The candidate is a pipeline definition (``pipelines/definitions/NAME.json``),
normally the baseline's ID plus changes; new step variants and prompts go
into the libraries.

A round runs the candidate on the round's keys, saving every step's output.
Keys apply at the focus, by default where runs start. A focus deeper in the
tree tests a change before it: runs start early enough to include the change,
every step before the focus runs on everything (from the caches, so a code
step such as grouping costs nothing), and only the focused step is limited
to the keys. A round's keys are a list, ``changed`` (every case whose input
at the focus differs from the baseline's, plus cases no longer there) or
``all``. Cases whose input at the focus is unchanged can still change, by
chance or because a step batches cases; the comparison marks them.

The baseline side is read, never run: from the baseline's complete run, one
over everything from the same start on the same inputs with every step saved.
Its final results give the comparison and its step outputs the step-by-step
diff. A confirm round over every case leaves such a run for its candidate, so
each adopted pipeline has one; otherwise the round asks for one.

A plan's dry run is kept under ``data/estimates``, apart from the runs that
process data.

Rounds are ``probe`` (the cases the change targets; cheap) and ``confirm``
(the whole reference set, the confirm keys, or every case). Adopting moves a tag to the
candidate after a confirm round that finished every case; cases that got worse
need an explicit acceptance, which the tag log records.
"""
from datetime import datetime, timezone
from pathlib import Path
import re

from court.config import paths
from court.core.datasets import Datasets, diff as dataset_diff
from court.core.hashing import read_json, save_json
from court.core.pipelines import Pipelines, diff as pipeline_diff, find
from court.core.runner import Runner
from court.lab import compare

NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
ROUNDS = ('probe', 'confirm')


class ExperimentError(Exception):
    pass


class Regressions(ExperimentError):
    """The confirm round has cases that got worse; ``cases`` lists each with its chips."""

    def __init__(self, cases):
        self.cases = cases
        super().__init__(f'{len(cases)} cases got worse in the confirm round')


class Lab:
    def __init__(self, root=None, pipelines=None, datasets=None, runner=None):
        self.root = Path(root or paths().experiments)
        self.pipelines = pipelines or Pipelines()
        self.datasets = datasets or Datasets()
        self.runner = runner or Runner(self.pipelines, self.datasets)

    def folder(self, name):
        if not NAME.fullmatch(name):
            raise ExperimentError('Experiment names use lowercase letters, numbers and dashes')
        return self.root/name

    def load(self, name):
        path = self.folder(name)/'experiment.json'
        if not path.exists():
            raise ExperimentError(f'No experiment {name}')
        return read_json(path)

    def all(self):
        return [read_json(p) for p in sorted(self.root.glob('*/experiment.json'))]

    def rounds(self, name):
        folder = self.folder(name)/'rounds'
        return {r: read_json(folder/f'{r}.json') for r in ROUNDS if (folder/f'{r}.json').exists()}

    # Creating ---------------------------------------------------------------
    def new(self, name, hypothesis, baseline='production', start='', inputs=None, probe=(), confirm=None,
            reference=None, changes=(), focus=None):
        """Create the experiment and its candidate definition (the baseline plus ``changes``).

        ``probe`` is a list of keys or ``changed``; ``confirm`` a list, ``reference`` or ``all``.
        """
        folder = self.folder(name)
        if folder.exists():
            raise ExperimentError(f'Experiment {name} exists')
        base = self.pipelines.load(baseline)
        definition = self.pipelines.root/'definitions'/f'{name}.json'
        if definition.exists():
            raise ExperimentError(f'A pipeline definition named {name} exists')
        focus = focus if focus not in (None, start) else None
        if focus is not None:
            if start and not focus.startswith(start+'/'):
                raise ExperimentError(f'The focus {focus} is not inside {start}')
            find(base['root'], focus)
        if probe == 'changed' and focus is None:
            raise ExperimentError('Choosing changed cases needs a focus after where runs start')
        pinned = {k: self.datasets.open(v).id for k, v in (inputs or {}).items()}
        record = {'name': name, 'hypothesis': hypothesis, 'baseline': base['id'],
                  'baseline_tag': None if baseline.startswith('p-') else baseline, 'pipeline': name,
                  'start': start, 'inputs': pinned, 'focus': focus,
                  'probe': probe if isinstance(probe, str) else sorted(probe), 'confirm': confirm,
                  'reference': self.datasets.open(reference).id if reference else None,
                  'created_at': datetime.now(timezone.utc).isoformat()}
        save_json(definition, {'name': name, 'description': hypothesis, 'base': base['id'], 'changes': list(changes)})
        save_json(folder/'experiment.json', record)
        return record

    # Planning and running ------------------------------------------------------
    def candidate(self, record):
        return self.pipelines.resolve(self.pipelines.definition(record['pipeline']))

    def focus(self, record):
        return record.get('focus') or record['start']

    def keys(self, record, round_name):
        """The round's keys at the focus: a sorted list, ``changed`` or ``all``."""
        if round_name not in ROUNDS:
            raise ExperimentError(f'Rounds are {", ".join(ROUNDS)}')
        if round_name == 'probe':
            keys = record['probe']
        elif record.get('confirm') == 'reference' or record.get('confirm') is None and record.get('reference'):
            keys = self.datasets.open(record['reference']).keys()
        else:
            keys = record.get('confirm') or []
        if keys in ('changed', 'all'):
            return keys
        if not keys:
            raise ExperimentError(f'The {round_name} round has no cases')
        return sorted(keys)

    def baseline_run(self, record, base):
        """The baseline's complete run: over everything from the experiment's start, every step saved.

        A round reads the baseline side from it rather than running the baseline again. Each
        confirm round over every case leaves one for its candidate, which adopting makes production.
        """
        start = record['start']
        for run in sorted(self.runner.all(), key=lambda r: r.get('finished_at') or '', reverse=True):
            if run['pipeline'] == base['id'] and run['keys'] is None and not run.get('focus') \
                    and run['options'].get('save_all') and run.get('status') in ('complete', 'stopped') \
                    and self.runner.inputs_at(run, start) == record['inputs']:
                return run
        command = ' '.join(['python -m court run NAME', base['id'], '--save-all',
                            *([f'--at {start}'] if start else []),
                            *(f'--input {n}={d}' for n, d in sorted(record['inputs'].items()))])
        raise ExperimentError(f'The baseline {base["id"]} has no complete run with every step saved to compare '
                              f'with. Run it once over everything (free from the cache when its calls are saved): '
                              f'{command}')

    def baseline_inputs(self, record, full):
        """The datasets the baseline's complete run fed to the focus."""
        focus, start = self.focus(record), record['start']
        if focus == start:
            return dict(record['inputs'])
        found = self.runner.inputs_at(full, focus)
        if not found:
            raise ExperimentError(f'Run {full["name"]} did not save what it fed to {focus}')
        return found

    def limits(self, record, keys, base_inputs):
        """Runner arguments for a round's keys: keys, focus and the datasets to find changes against."""
        focus = self.focus(record)
        if keys == 'all':
            return {}
        if keys == 'changed':
            return {'focus': focus, 'changed_from': base_inputs}
        return {'keys': keys, 'focus': focus}

    def input_changes(self, before, after):
        """Per key, how its input at the focus differs between two runs: changed, added or removed."""
        found = {}
        for name in sorted(set(before) | set(after)):
            if before.get(name) == after.get(name) or name not in before or name not in after:
                continue
            counts = dataset_diff(self.datasets.open(before[name]), self.datasets.open(after[name]))
            for change in ('changed', 'added', 'removed'):
                for key in counts[change]:
                    found.setdefault(key, change)
        return found

    def plan(self, name, round_name='probe'):
        """The definition diff and a dry-run cost estimate; no model calls."""
        record = self.load(name)
        candidate, base = self.candidate(record), self.pipelines.load(record['baseline'])
        keys = self.keys(record, round_name)
        base_inputs = self.baseline_inputs(record, self.baseline_run(record, base))
        # The dry run is working material, kept apart from the runs that process data.
        estimates = Runner(self.pipelines, self.datasets, self.runner.root.parent/'estimates', self.runner.store,
                           self.runner.cache, self.runner.raw)
        dry = estimates.run(f'{name}-{round_name}-{candidate["id"][2:10]}', candidate['id'], record['inputs'],
                            at=record['start'], mode='dry', **self.limits(record, keys, base_inputs))
        focused = dry.get('focused') or {}
        cases = focused.get('keys', keys if isinstance(keys, list) else None)
        plan = {'experiment': name, 'round': round_name, 'focus': self.focus(record) or '(root)',
                'keys': len(cases) if cases is not None else dry['items']['total'], 'removed': focused.get('removed', []),
                'candidate': candidate['id'], 'baseline': base['id'], 'changes': pipeline_diff(base, candidate),
                'estimate': dry['cost'], 'not_sent': dry['items']['failed'].get('not_sent', 0),
                'planned_at': datetime.now(timezone.utc).isoformat()}
        save_json(self.folder(name)/'plans'/f'{round_name}.json', plan)
        return plan

    def plans(self, name):
        folder = self.folder(name)/'plans'
        return {r: read_json(folder/f'{r}.json') for r in ROUNDS if (folder/f'{r}.json').exists()}

    def run(self, name, round_name='probe', mode='send', limit=0.0, resamples=0, resend_failed=False, workers=4,
            transport=None):
        record = self.load(name)
        candidate, base = self.candidate(record), self.pipelines.load(record['baseline'])
        keys, focus = self.keys(record, round_name), self.focus(record)
        full = self.baseline_run(record, base)
        base_inputs = self.baseline_inputs(record, full)
        options = {'mode': mode, 'limit': limit, 'resamples': resamples, 'resend_failed': resend_failed,
                   'workers': workers, 'transport': transport}
        run = self.runner.run(f'{name}-{round_name}-{candidate["id"][2:10]}', candidate['id'], record['inputs'], at=record['start'],
                              save_all=True, note=f'Experiment {name}, {round_name} round',
                              labels={'experiment': name, 'round': round_name, 'side': 'candidate'},
                              **self.limits(record, keys, base_inputs), **options)
        output = self.datasets.open(run['output'])
        changes = self.input_changes(base_inputs, self.runner.inputs_at(run, focus) or base_inputs)
        removed = sorted(k for k, change in changes.items() if change == 'removed')
        chosen = (run.get('focused') or {}).get('keys') if keys != 'all' else output.keys()
        cases = sorted(set(chosen if chosen is not None else keys) | set(removed))
        changes = {k: changes[k] for k in cases if k in changes}
        # Keys new in the candidate's input have no baseline result to find.
        known = [k for k in cases if changes.get(k) != 'added']
        results = self.datasets.open(full['outputs'].get(record['start']) or full['output'])
        baseline_results = results.load([k for k in known if k in results])
        reference = self.datasets.open(record['reference']).load(cases) if record.get('reference') else None
        # The baseline's run is finished: its unsent calls are failures, not work left to do.
        comparison = compare.compare(baseline_results, output.load(), reference, cases, settled=True, inputs=changes)

        def limited(path):
            # Positions where only the round's cases ran: the focus, inside it, and the steps holding it.
            return keys != 'all' and (focus == '' or path == '' or path == focus or path.startswith(focus+'/')
                                      or focus.startswith(path+'/'))
        steps, wanted = {}, set(cases)
        for path in sorted(set(run['outputs']) & set(full['outputs'])):
            before, after = self.datasets.open(full['outputs'][path]).index, self.datasets.open(run['outputs'][path]).index
            if limited(path):
                before = {k: v for k, v in before.items() if k in wanted}
                after = {k: v for k, v in after.items() if k in wanted}
            counts = dataset_diff(before, after)
            steps[path or '(root)'] = {k: len(v) for k, v in counts.items()} | {'changed_keys': counts['changed'][:50]}
        result = {'experiment': name, 'round': round_name, 'keys': cases, 'selection': keys, 'focus': focus,
                  'inputs': changes,
                  'candidate': {'pipeline': candidate['id'], 'run': run['name'], 'output': run['output']},
                  'baseline': {'pipeline': base['id'], 'run': full['name'], 'output': results.id, 'settled': True},
                  'changes': pipeline_diff(base, candidate), 'summary': comparison['summary'],
                  'accuracy': comparison['accuracy'], 'regressions': compare.regressions(comparison),
                  'unfinished': compare.unfinished(comparison),
                  'cases': [{k: c[k] for k in ('case_id', 'verdict', 'chips', 'processing', 'highlights', 'input')}
                            for c in comparison['cases']],
                  'steps': steps, 'cost': run['cost'], 'status': run['status'],
                  'finished_at': datetime.now(timezone.utc).isoformat()}
        save_json(self.folder(name)/'rounds'/f'{round_name}.json', result)
        return result

    def adopt(self, name, tag='production', note='', accept_regressions=False):
        """Move a tag to the experiment's candidate after its confirm round.

        Raises ``Regressions`` when cases got worse, unless ``accept_regressions``.
        """
        record, rounds = self.load(name), self.rounds(name)
        confirm = rounds.get('confirm')
        if confirm is None:
            raise ExperimentError('Run a confirm round before adopting')
        candidate = self.candidate(record)
        if confirm['candidate']['pipeline'] != candidate['id']:
            raise ExperimentError('The candidate changed after its confirm round; run the confirm round again')
        if confirm['status'] != 'complete' or confirm.get('unfinished'):
            raise ExperimentError('The confirm round did not finish every case; run it again, sending the calls it needs')
        if confirm['regressions'] and not accept_regressions:
            chips = {c['case_id']: c['chips'] for c in confirm['cases']}
            raise Regressions([{'case_id': k, 'chips': chips.get(k, [])} for k in confirm['regressions']])
        evidence = {'experiment': name, 'round': 'confirm', 'cases': len(confirm['keys']),
                    'headline': confirm['summary']['headline'], 'verdicts': confirm['summary']['verdicts'],
                    'accuracy': confirm['accuracy'], 'regressions': confirm['regressions']}
        return self.pipelines.set_tag(tag, candidate['id'], note=note or record['hypothesis'], evidence=evidence)
