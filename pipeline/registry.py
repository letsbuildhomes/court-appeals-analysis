"""Pipeline identities, frozen snapshots, tags and diffs.

A pipeline's ID is the hash of its ``describe()`` tree, which includes every
step's configuration, full prompt texts and the hashes of the source files
that implement it. The same tree and code always give the same ID, whatever
it is called. ``freeze`` stores the descriptor and copies of those source
files once under ``data/pipelines/<id>/``; snapshots are never changed.

Tags are names for pipelines, kept in an append-only log in git
(``pipeline/tags.jsonl``). A tag's current target is its latest entry, so the
log is also the history of what each tag meant. Commands accept a tag or an ID
and record the resolved ID, so moving a tag never changes earlier comparisons.

A pipeline records how to rebuild it: a module with options, or an experiment
applied to a base.
"""
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from config import ROOT

PIPELINES = ROOT/'data/pipelines'
TAGS = ROOT/'pipeline/tags.jsonl'
EXPERIMENTS = ROOT/'experiments'
PIPELINE_VERSION = 'appeal-pipeline-1'
PIPELINE_ID = re.compile(r'p-[0-9a-f]{12}\Z')
TAG = re.compile(r'[a-z][a-z0-9_.-]*\Z')
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def identify(tree):
    """``(id, descriptor)`` for a tree."""
    descriptor = tree.describe()
    return 'p-'+hashlib.sha256(canonical(descriptor).encode()).hexdigest()[:12], descriptor


def code_files(descriptor):
    files = dict(descriptor.get('code') or {})
    for child in descriptor.get('children', []):
        files.update(code_files(child))
    return files


def freeze(tree, source, root=None):
    """Store a tree's descriptor and source files once; return its ID."""
    pipeline_id, descriptor = identify(tree)
    folder = Path(root or PIPELINES)/pipeline_id
    if (folder/'pipeline.json').exists():
        if json.loads((folder/'pipeline.json').read_text())['descriptor'] != descriptor:
            raise ValueError(f'Stored pipeline {pipeline_id} has a different descriptor')
        return pipeline_id
    files = code_files(descriptor)
    folder.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f'.{pipeline_id}-', dir=folder.parent))
    try:
        for name, digest in files.items():
            raw = (ROOT/name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f'{name} changed while freezing the pipeline')
            # Files outside the repository keep their absolute path under sources/.
            target = temporary/'sources'/name.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        record = {'schema_version': PIPELINE_VERSION, 'id': pipeline_id, 'name': tree.name,
                  'created_at': datetime.now(timezone.utc).isoformat(), 'source': source,
                  'commit': git_commit(), 'descriptor': descriptor}
        (temporary/'pipeline.json').write_text(json.dumps(record, indent=2, ensure_ascii=False)+'\n')
        try:
            temporary.rename(folder)
        except OSError:
            if not (folder/'pipeline.json').exists():
                raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return pipeline_id


def load(pipeline_id, root=None):
    path = Path(root or PIPELINES)/pipeline_id/'pipeline.json'
    if not PIPELINE_ID.fullmatch(pipeline_id) or not path.exists():
        raise KeyError(f'No stored pipeline {pipeline_id}')
    record = json.loads(path.read_text())
    if record.get('schema_version') != PIPELINE_VERSION:
        raise ValueError('Unsupported pipeline record version')
    return record


def stored(root=None):
    root = Path(root or PIPELINES)
    return [load(p.name, root) for p in sorted(root.iterdir()) if PIPELINE_ID.fullmatch(p.name)] if root.is_dir() else []


def git_commit():
    try:
        run = lambda *a: subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        return {'commit': run('rev-parse', 'HEAD'), 'dirty': bool(run('status', '--porcelain', '--untracked-files=no'))}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'dirty': None}


# Tags ----------------------------------------------------------------------
def tag_log(path=None):
    path = Path(path or TAGS)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def tags(path=None):
    """Each tag's current pipeline ID."""
    current = {}
    for entry in tag_log(path):
        current[entry['tag']] = entry['pipeline']
    return current


def history(tag, path=None):
    return [e for e in tag_log(path) if e['tag'] == tag]


def set_tag(tag, pipeline_id, note, evidence=None, path=None, root=None):
    """Point a tag at a stored pipeline; earlier entries are kept."""
    if not TAG.fullmatch(tag) or PIPELINE_ID.fullmatch(tag):
        raise ValueError('Tags use lowercase letters, numbers, dots, dashes and underscores')
    load(pipeline_id, root)
    if not note or not note.strip():
        raise ValueError('Say why the tag moves')
    entry = {'tag': tag, 'pipeline': pipeline_id, 'at': datetime.now(timezone.utc).isoformat(),
             'note': note.strip(), 'evidence': evidence or {}, 'commit': git_commit()['commit']}
    path = Path(path or TAGS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False)+'\n')
    return entry


def resolve(ref, path=None):
    """A pipeline ID from an ID or a tag."""
    if PIPELINE_ID.fullmatch(ref):
        return ref
    current = tags(path)
    if ref not in current:
        raise KeyError(f'Unknown pipeline or tag {ref!r}')
    return current[ref]


# Building ------------------------------------------------------------------
def load_experiment(name, folder=None):
    """Import ``experiments/NAME/experiment.py`` as a package, so it can import its step files."""
    if not SAFE.fullmatch(name):
        raise ValueError('Invalid experiment name')
    folder = Path(folder or EXPERIMENTS)/name
    module_name = f'experiments.{name}'
    for key in [k for k in sys.modules if k == module_name or k.startswith(module_name+'.')]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(module_name, folder/'experiment.py',
                                                  submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def build(source, experiments=None):
    """Rebuild a tree from its recorded source."""
    if 'module' in source:
        return importlib.import_module(source['module']).build(**source.get('options', {}))
    if 'experiment' in source:
        base = build(source['base'], experiments)
        return load_experiment(source['experiment'], experiments).build(base)
    raise ValueError('Unknown pipeline source')


# Diffs ---------------------------------------------------------------------
COMPARED = ('type', 'description', 'model', 'conversation', 'selector', 'params', 'reads', 'writes', 'fields',
            'max_group')


def steps(descriptor):
    """Every step descriptor by path, in tree order (the root's path is empty)."""
    found = {descriptor['path']: descriptor}
    for child in descriptor.get('children', []):
        found.update(steps(child))
    return found


def diff(base, candidate):
    """Step-by-step differences between two descriptors.

    Each row has a status (added, removed, changed, unchanged), the changed
    configuration fields, prompt changes with both texts, changed source files,
    and whether its children's order changed.
    """
    before, after = steps(base) if base else {}, steps(candidate) if candidate else {}
    order = list(after) + [p for p in before if p not in after]
    rows = []
    for path in order:
        b, c = before.get(path), after.get(path)
        if b is None or c is None:
            rows.append({'path': path, 'name': (b or c)['name'], 'status': 'added' if b is None else 'removed'})
            continue
        fields = [{'field': f, 'base': b.get(f), 'value': c.get(f)} for f in COMPARED if b.get(f) != c.get(f)]
        prompts = []
        for name in sorted(set(b['prompts']) | set(c['prompts'])):
            pb, pc = b['prompts'].get(name), c['prompts'].get(name)
            if (pb or {}).get('sha256') != (pc or {}).get('sha256'):
                prompts.append({'name': name, 'status': 'added' if pb is None else 'removed' if pc is None else 'changed',
                                'base': (pb or {}).get('text'), 'value': (pc or {}).get('text')})
        code = sorted(n for n in set(b['code']) | set(c['code']) if b['code'].get(n) != c['code'].get(n))
        reordered = [x['name'] for x in b['children']] != [x['name'] for x in c['children']] and \
            {x['name'] for x in b['children']} == {x['name'] for x in c['children']}
        changed = bool(fields or prompts or code or reordered)
        rows.append({'path': path, 'name': c['name'], 'status': 'changed' if changed else 'unchanged',
                     'fields': fields, 'prompts': prompts, 'code': code, 'reordered': reordered})
    return rows


def summary(rows):
    """Short descriptions of what changed, for lists and cards."""
    result = []
    for row in rows:
        label = row['path'] or 'pipeline'
        if row['status'] in ('added', 'removed'):
            result.append(f'{label}: {row["status"]}')
        elif row['status'] == 'changed':
            parts = [f'{p["name"]} prompt' for p in row['prompts']] + [f['field'] for f in row['fields']]
            if row['code']:
                parts.append('code')
            if row['reordered']:
                parts.append('order')
            result.append(f'{label}: '+', '.join(parts))
    return result
