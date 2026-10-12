"""Experiments: a hypothesis, a candidate pipeline and its rounds of evidence.

An experiment is a directory ``experiments/NAME/``:

    experiment.json      name, hypothesis, the pinned baseline pipeline ID (and the tag it came from),
                         the candidate's definition name, the position runs start at, the input
                         datasets for that position, the probe keys, the confirm keys and an optional
                         reference dataset
    rounds/ROUND.json    one record per round run: the keys, both pipelines and runs, the definition
                         diff, the comparison summary and every case's verdict, step-by-step data diffs,
                         regressions and cost

The candidate is a pipeline definition (``pipelines/definitions/NAME.json``),
normally the baseline's ID plus changes; new step variants and prompts go
into the libraries.

A round runs the candidate on the round's keys, saving every step's output.
The baseline's results come from an existing run of the baseline pipeline
that covers those keys with the same inputs (production's run, for example);
otherwise the baseline is run on the keys from the cache, without sending.
The baseline is also run on the keys with every step saved, from the cache,
so the two pipelines can be compared step by step.

Rounds are ``probe`` (the cases the change targets; cheap) and ``confirm``
(the whole reference set, or the confirm keys). Adopting moves a tag to the
candidate and requires a confirm round without regressions.
"""
from datetime import datetime, timezone
from pathlib import Path
import re

from court.config import paths
from court.core.datasets import Datasets, diff as dataset_diff
from court.core.hashing import read_json, save_json
from court.core.pipelines import Pipelines, diff as pipeline_diff
from court.core.runner import Runner
from court.lab import compare

NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
ROUNDS = ('probe', 'confirm')


class ExperimentError(Exception):
    pass


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
            reference=None, changes=()):
        """Create the experiment and its candidate definition (the baseline plus ``changes``)."""
        folder = self.folder(name)
        if folder.exists():
            raise ExperimentError(f'Experiment {name} exists')
        base = self.pipelines.load(baseline)
        definition = self.pipelines.root/'definitions'/f'{name}.json'
        if definition.exists():
            raise ExperimentError(f'A pipeline definition named {name} exists')
        pinned = {k: self.datasets.open(v).id for k, v in (inputs or {}).items()}
        record = {'name': name, 'hypothesis': hypothesis, 'baseline': base['id'],
                  'baseline_tag': None if baseline.startswith('p-') else baseline, 'pipeline': name,
                  'start': start, 'inputs': pinned, 'probe': sorted(probe), 'confirm': confirm,
                  'reference': self.datasets.open(reference).id if reference else None,
                  'created_at': datetime.now(timezone.utc).isoformat()}
        save_json(definition, {'name': name, 'description': hypothesis, 'base': base['id'], 'changes': list(changes)})
        save_json(folder/'experiment.json', record)
        return record

    # Planning and running ------------------------------------------------------
    def candidate(self, record):
        return self.pipelines.resolve(self.pipelines.definition(record['pipeline']))

    def keys(self, record, round_name):
        if round_name not in ROUNDS:
            raise ExperimentError(f'Rounds are {", ".join(ROUNDS)}')
        if round_name == 'probe':
            keys = record['probe']
        elif record.get('confirm') == 'reference' or record.get('confirm') is None and record.get('reference'):
            keys = self.datasets.open(record['reference']).keys()
        else:
            keys = record.get('confirm') or []
        if not keys:
            raise ExperimentError(f'The {round_name} round has no cases')
        return sorted(keys)

    def plan(self, name, round_name='probe'):
        """The definition diff and a dry-run cost estimate; no model calls."""
        record = self.load(name)
        candidate, base = self.candidate(record), self.pipelines.load(record['baseline'])
        keys = self.keys(record, round_name)
        dry = self.runner.run(f'{name}-{round_name}-plan-{candidate["id"][2:10]}', candidate['id'], record['inputs'], at=record['start'],
                              keys=keys, mode='dry', labels={'experiment': name, 'round': round_name, 'side': 'plan'})
        baseline_run, _ = self.runner.find_output(base['id'], record['start'], record['inputs'], keys)
        return {'experiment': name, 'round': round_name, 'keys': len(keys), 'candidate': candidate['id'],
                'baseline': base['id'], 'changes': pipeline_diff(base, candidate),
                'estimate': dry['cost'], 'not_sent': dry['items']['failed'].get('not_sent', 0),
                'baseline_source': baseline_run['name'] if baseline_run else 'a cached run of the baseline'}

    def run(self, name, round_name='probe', mode='send', limit=0.0, resamples=0, resend_failed=False, workers=4,
            transport=None):
        record = self.load(name)
        candidate, base = self.candidate(record), self.pipelines.load(record['baseline'])
        keys = self.keys(record, round_name)
        options = {'mode': mode, 'limit': limit, 'resamples': resamples, 'resend_failed': resend_failed,
                   'workers': workers, 'transport': transport}
        run = self.runner.run(f'{name}-{round_name}-{candidate["id"][2:10]}', candidate['id'], record['inputs'], at=record['start'],
                              keys=keys, save_all=True, note=f'Experiment {name}, {round_name} round',
                              labels={'experiment': name, 'round': round_name, 'side': 'candidate'}, **options)
        trace = self.runner.run(f'{name}-{round_name}-baseline-{base["id"][2:10]}', base['id'], record['inputs'], at=record['start'],
                                keys=keys, save_all=True, mode='cached', workers=workers,
                                note=f'Baseline of experiment {name}, {round_name} round, from the cache',
                                labels={'experiment': name, 'round': round_name, 'side': 'baseline'})
        found, results = self.runner.find_output(base['id'], record['start'], record['inputs'], keys)
        baseline_results = results.load(keys) if results else self.datasets.open(trace['output']).load()
        reference = self.datasets.open(record['reference']).load(keys) if record.get('reference') else None
        comparison = compare.compare(baseline_results, self.datasets.open(run['output']).load(), reference, keys)
        steps = {}
        for path in sorted(set(run['outputs']) & set(trace['outputs'])):
            counts = dataset_diff(self.datasets.open(trace['outputs'][path]), self.datasets.open(run['outputs'][path]))
            steps[path or '(root)'] = {k: len(v) for k, v in counts.items()} | {'changed_keys': counts['changed'][:50]}
        result = {'experiment': name, 'round': round_name, 'keys': keys,
                  'candidate': {'pipeline': candidate['id'], 'run': run['name'], 'output': run['output']},
                  'baseline': {'pipeline': base['id'], 'run': found['name'] if found else trace['name'],
                               'output': results.id if results else trace['output'], 'trace_run': trace['name']},
                  'changes': pipeline_diff(base, candidate), 'summary': comparison['summary'],
                  'accuracy': comparison['accuracy'], 'regressions': compare.regressions(comparison),
                  'unfinished': compare.unfinished(comparison),
                  'cases': [{k: c[k] for k in ('case_id', 'verdict', 'chips', 'processing', 'highlights')}
                            for c in comparison['cases']],
                  'steps': steps, 'cost': run['cost'], 'status': run['status'],
                  'finished_at': datetime.now(timezone.utc).isoformat()}
        save_json(self.folder(name)/'rounds'/f'{round_name}.json', result)
        return result

    def adopt(self, name, tag='production', note='', accept_regressions=False):
        """Move a tag to the experiment's candidate after a confirm round without regressions."""
        record, rounds = self.load(name), self.rounds(name)
        confirm = rounds.get('confirm')
        if confirm is None:
            raise ExperimentError('Run a confirm round before adopting')
        candidate = self.candidate(record)
        if confirm['candidate']['pipeline'] != candidate['id']:
            raise ExperimentError('The candidate changed after its confirm round; run the confirm round again')
        if confirm['regressions'] and not accept_regressions:
            raise ExperimentError(f'{len(confirm["regressions"])} cases regressed in the confirm round; '
                                  'accept them explicitly with a note to adopt anyway')
        if confirm['status'] != 'complete' or confirm.get('unfinished'):
            raise ExperimentError('The confirm round did not finish every case; run it again, sending the calls it needs')
        evidence = {'experiment': name, 'round': 'confirm', 'cases': len(confirm['keys']),
                    'headline': confirm['summary']['headline'], 'verdicts': confirm['summary']['verdicts'],
                    'accuracy': confirm['accuracy'], 'regressions': confirm['regressions']}
        return self.pipelines.set_tag(tag, candidate['id'], note=note or record['hypothesis'], evidence=evidence)
