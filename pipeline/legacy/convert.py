"""Convert a legacy appeals-1 run's saved outputs into a converted run.

The converted run reads its sources from the legacy run itself and records
the pipeline that describes it: the ported legacy pipeline built with the
legacy run's frozen prompts and settings. No model is called. Legacy statuses
become processing statuses; scope becomes the disposition; finalized v6
records become contract records.
"""
import hashlib
import inspect
import json
from pathlib import Path

import appeal_schema
from appeal_runtime import read, save
from pipeline import legacy, registry, runs
from pipeline.corpus import Corpus
from pipeline.legacy import v6

FAILED = {'review_failed', 'duration_failed', 'incomplete', 'too_large'}


def options(manifest, prompts):
    """``build`` options that differ from the defaults, from a legacy manifest."""
    defaults = {k: p.default for k, p in inspect.signature(legacy.build).parameters.items()}
    current = {name: p.text for name, p in legacy.prompts().items()}
    values = {'prompt_overrides': {k: v for k, v in prompts.items() if current.get(k) != v} or None,
              'model': manifest.get('model', 'deepseek-flash'), 'reasoning': manifest.get('reasoning', 'high'),
              'max_output_tokens': manifest.get('max_output_tokens', 65536),
              'review_max_output_tokens': manifest.get('review_max_output_tokens'),
              'review_batch_cases': manifest.get('review_batch_size', 2),
              'review_batch_chars': manifest.get('review_batch_chars', 200000),
              'max_chars': manifest.get('max_chars', 600000)}
    return {k: v for k, v in values.items() if v != defaults[k]}


def outcome(case_id, saved, pages):
    """``(result, status)`` for one legacy checkpoint."""
    state = saved.get('status', 'not_run')
    warnings = list(saved.get('warnings', []))
    disposition = v6.scope_disposition(saved['triage'], saved.get('route') or {}) if saved.get('triage') else None
    records = []
    if state == 'scope_flagged':
        disposition = v6.flagged_disposition(disposition, saved.get('review') or {})
    if state in ('complete', 'scope_flagged'):
        processing = 'complete'
        if state == 'complete' and saved.get('candidate'):
            records, notes = v6.records(saved['candidate'], saved.get('duration_bounds'))
            warnings.extend(notes)
    elif state in FAILED:
        processing = 'failed'
        if state == 'too_large':
            disposition = None
    else:
        processing = 'in_progress'
    errors = [{'step': state, 'error': saved['error'], 'type': saved.get('error_type') or state, 'retryable': True}] \
        if saved.get('error') else []
    if processing == 'failed' and not errors:
        errors = [{'step': state, 'error': saved.get('reason') or state, 'type': state, 'retryable': state != 'too_large'}]
    result = appeal_schema.result(case_id, disposition, records)
    problems = appeal_schema.validate(result)
    if problems:
        raise ValueError(f'{case_id}: converted result violates the contract: '+'; '.join(problems[:3]))
    checked, unverified = appeal_schema.verify_quotes(result, pages)
    status = {'state': processing, 'errors': errors, 'warnings': warnings, 'legacy_status': state,
              'quotes': {'checked': checked, 'unverified': unverified}}
    return result, status


def convert(legacy_folder, folder, name):
    """Write a converted run for a legacy run; return its run record."""
    from appeal_evidence import source_pages
    legacy_folder, folder = Path(legacy_folder).resolve(), Path(folder)
    manifest, prompts = read(legacy_folder/'manifest.json'), read(legacy_folder/'prompts.json')
    if manifest.get('schema_version') != 'appeals-1':
        raise ValueError('Not a legacy appeals-1 run')
    if hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest() != manifest['prompt_sha256']:
        raise ValueError('Frozen prompts changed')
    source = {'module': 'pipeline.legacy', 'options': options(manifest, prompts)}
    pipeline_id = registry.freeze(registry.build(source), source)
    corpus = Corpus(legacy_folder)
    calls = [read(p) for p in (legacy_folder/'calls').rglob('*.result.json')]
    meta = runs.create(folder, name=name, pipeline_id=pipeline_id, case_ids=corpus.ids(), corpus=corpus,
                       kind='converted', source_run=str(legacy_folder))
    outputs = {}
    for case_id in corpus.ids():
        saved = None
        for sub in ('results', 'first_pass'):
            path = legacy_folder/sub/f'{case_id}.json'
            if path.exists():
                saved = read(path)
                break
        result, status = outcome(case_id, saved or {'status': 'not_run'}, source_pages(corpus.text(case_id)))
        outputs[case_id] = (result, status, None)
    runs.write(folder, outputs)
    meta['cost'] = {'new_usd': 0, 'recorded_usd': sum(c.get('charged_or_reserved_usd') or 0 for c in calls),
                    'reused_usd': None, 'note': 'Charges the legacy run recorded, including retried attempts'}
    meta['sealed_at'] = meta['created_at']
    save(folder/'run.json', meta)
    return meta
