"""Freeze and inspect baseline experiment artifacts without executing a model.

The input is an existing run over frozen extracted text. This module neither
selects a cohort nor labels a reference set. Evaluators consume ``cases`` from
``read_experiment``; execution-specific drafts remain in the canonical artifact.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from appeal_contracts import adapt_run

EXPERIMENT_VERSION = 'appeal-experiment-1'


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def create_experiment(run_dir, output, *, name=None, code_revision=None,
                      verify_sources=True, case_ids=None, kind='historical_baseline',
                      execution=None, cost=None, experiment=None):
    """Publish a new immutable artifact directory; never replace one.

    ``code_revision`` describes the adapter invocation, not the historical model
    run. Its default is unknown rather than attributing current code to old work.
    ``case_ids`` restricts the artifact to a cohort, in run order. Experiment
    runners supply ``kind``, ``execution``, ``cost`` and ``experiment`` metadata.
    """
    if kind not in ('historical_baseline', 'experiment_candidate'):
        raise ValueError('Unknown experiment artifact kind')
    run_dir, output = Path(run_dir).resolve(), Path(output).resolve()
    if output == run_dir or run_dir in output.parents:
        raise ValueError('Experiment output must be outside the source run')
    if output.exists():
        raise ValueError('Experiment output already exists')
    canonical = adapt_run(run_dir, verify_sources=verify_sources)
    if case_ids is not None:
        wanted = set(case_ids)
        if len(wanted) != len(case_ids):
            raise ValueError('Duplicate selected case')
        missing = wanted - {case['case_id'] for case in canonical['cases']}
        if missing:
            raise ValueError('Selected cases absent from run: '+', '.join(sorted(missing)))
        canonical['cases'] = [case for case in canonical['cases'] if case['case_id'] in wanted]
    if cost is not None:
        canonical['provenance']['cost'] = cost
    raw = _bytes(canonical)
    manifest = {
        'schema_version': EXPERIMENT_VERSION,
        'name': name or output.name,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'kind': kind,
        'input_boundary': 'frozen_extracted_text',
        'adapter_revision': code_revision,
        'source_run': str(run_dir),
        'recipe': canonical['recipe'],
        'artifacts': {'cases': {'path': 'cases.json', 'sha256': _hash(raw),
                                'schema_version': canonical['schema_version']}},
        'case_count': len(canonical['cases']),
        'execution': execution or {'performed': False, 'new_model_calls': 0,
                                   'new_model_cost_usd': 0},
        'reference': None,
    }
    if case_ids is not None:
        manifest['case_selection'] = sorted(case_ids)
    if experiment is not None:
        manifest['experiment'] = experiment
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+output.name+'-', dir=output.parent))
    try:
        (temporary/'cases.json').write_bytes(raw)
        (temporary/'manifest.json').write_bytes(_bytes(manifest))
        # mkdir reserves the destination without allowing a concurrent overwrite.
        output.mkdir()
        try:
            for file in temporary.iterdir():
                file.rename(output/file.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    finally:
        shutil.rmtree(temporary)
    return manifest


def read_experiment(folder):
    """Validate artifact identity and return manifest, canonical, and cases."""
    folder = Path(folder)
    manifest = json.loads((folder/'manifest.json').read_bytes())
    if manifest.get('schema_version') != EXPERIMENT_VERSION:
        raise ValueError('Unsupported experiment manifest version')
    artifact = manifest['artifacts']['cases']
    if artifact.get('path') != 'cases.json':
        raise ValueError('Invalid cases artifact path')
    raw = (folder/'cases.json').read_bytes()
    if _hash(raw) != artifact['sha256']:
        raise ValueError('Experiment cases artifact changed')
    canonical = json.loads(raw)
    if canonical.get('schema_version') != 'appeal-cases-1' or artifact.get('schema_version') != canonical['schema_version']:
        raise ValueError('Unsupported canonical cases version')
    cases = canonical['cases']
    if len(cases) != manifest['case_count']:
        raise ValueError('Experiment case count mismatch')
    identities = [case['case_id'] for case in cases]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate experiment case identity')
    return {'manifest': manifest, 'canonical': canonical, 'cases': cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    freeze = sub.add_parser('import-run', help='Adapt saved results; no model execution')
    freeze.add_argument('run', type=Path)
    freeze.add_argument('output', type=Path)
    freeze.add_argument('--name')
    freeze.add_argument('--code-revision')
    inspect = sub.add_parser('inspect', help='Validate and summarize an artifact')
    inspect.add_argument('folder', type=Path)
    args = parser.parse_args()
    if args.command == 'import-run':
        value = create_experiment(args.run, args.output, name=args.name,
                                  code_revision=args.code_revision)
    else:
        value = read_experiment(args.folder)['manifest']
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
