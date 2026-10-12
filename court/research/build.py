"""Build a research snapshot from a results map and the case set it came from."""
from court.config import paths
from court.core.datasets import Datasets
from court.research import snapshot


def build(results, cases=None, output=None):
    store = Datasets()
    found = store.open(results)
    if found.kind != 'results':
        raise ValueError(f'{found.id} is a {found.kind} dataset, not results')
    case_set = store.open(cases) if cases else _case_set(store, found)
    output = output or paths().data/'research'/found.id
    provenance = {'results': found.id, 'cases': case_set.id, 'pipeline': found.manifest['producer'].get('pipeline'),
                  'run': found.manifest['producer'].get('run'), 'contract': found.manifest['contract']}
    return snapshot.build(found.load(), case_set.load(), output, provenance)


def _case_set(store, results):
    """The case set a results map was produced from, when its producer recorded it."""
    inputs = results.manifest['producer'].get('inputs') or {}
    if 'cases' in inputs:
        return store.open(inputs['cases'])
    run = results.manifest['producer'].get('run')
    raise ValueError(f'Name the case set for {results.id} (from run {run}) with --cases')
