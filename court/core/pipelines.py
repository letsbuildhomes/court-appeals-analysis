"""Pipeline definitions, resolution into identified trees, tags and definition diffs.

A pipeline is data: a tree of nodes. A node has a ``name`` (its position in
the tree; positions are slash paths such as ``extract/finishing/review``),
the library ``step`` at that position, and optionally ``settings``,
``prompts`` (slot → prompt name), ``inputs`` (how a composite wires it),
``save`` (save its output as a dataset of that kind) and ``children``.

An authored definition (``pipelines/definitions/NAME.json``) is either a full
tree (``root``) or a base pipeline ID plus ``changes``:

    {"at": PATH, "replace": NODE}        put another node at a position
    {"at": PATH, "settings": {...}}      change settings (merged)
    {"at": PATH, "prompts": {...}}       change prompts (merged)
    {"at": PATH, "step": NAME}           swap the library entry, keeping the rest
    {"at": PATH, "remove": true}
    {"after": PATH, "insert": NODE}      or {"before": PATH, "insert": NODE}

Resolving a definition records the hash of every library entry and prompt
the tree uses, checks the tree against each entry's declarations, and
saves it under its ID in ``pipelines/resolved/``. The ID is the hash of the
tree and those hashes; names, descriptions, the git commit and the lockfile
hash are recorded beside it, not in it. Resolved trees are never changed.

Tags (``pipelines/tags.jsonl``) are an append-only log of names for IDs.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from court.config import ROOT, paths
from court.core.hashing import file_hash, read_json, save_json, value_hash
from court.core.library import Library, LibraryError, check_name
from court.core.step import INTERFACES

SCHEMA = 'pipeline-1'
NODE_NAME = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
NODE_KEYS = {'name', 'step', 'settings', 'prompts', 'inputs', 'save', 'children', 'description'}
TAG = re.compile(r'[a-z0-9][a-z0-9._-]*\Z')


class PipelineError(Exception):
    pass


# Paths in trees ---------------------------------------------------------
def walk(node, path=''):
    """Every ``(path, node)`` in a tree, depth first; the root's path is ''."""
    yield path, node
    for child in node.get('children', []):
        yield from walk(child, f'{path}/{child["name"]}' if path else child['name'])


def find(root, path):
    node = root
    for part in path.split('/') if path else []:
        node = next((c for c in node.get('children', []) if c['name'] == part), None)
        if node is None:
            raise PipelineError(f'No position {path!r}')
    return node


def _parent(root, path):
    parts = path.split('/')
    parent = find(root, '/'.join(parts[:-1]))
    index = next((i for i, c in enumerate(parent.get('children', [])) if c['name'] == parts[-1]), None)
    if index is None:
        raise PipelineError(f'No position {path!r}')
    return parent, index


def apply(root, changes):
    """A new tree with the changes applied."""
    root = copy.deepcopy(root)
    for change in changes:
        if 'insert' in change:
            anchor = change.get('after') or change.get('before')
            parent, index = _parent(root, anchor)
            parent['children'].insert(index+1 if 'after' in change else index, copy.deepcopy(change['insert']))
            continue
        path = change['at']
        if 'remove' in change:
            parent, index = _parent(root, path)
            parent['children'].pop(index)
        elif 'replace' in change:
            node = copy.deepcopy(change['replace'])
            node.setdefault('name', path.split('/')[-1])
            if path:
                parent, index = _parent(root, path)
                parent['children'][index] = node
            else:
                root = node
        else:
            node = find(root, path)
            for key in ('settings', 'prompts', 'inputs'):
                if key in change:
                    node[key] = {**node.get(key, {}), **change[key]}
            if 'step' in change:
                node['step'] = change['step']
            unknown = set(change) - {'at', 'settings', 'prompts', 'inputs', 'step'}
            if unknown:
                raise PipelineError(f'Unknown change {", ".join(sorted(unknown))} at {path!r}')
    return root


# Resolution -------------------------------------------------------------
class Pipelines:
    def __init__(self, root=None, library=None):
        self.root = Path(root or paths().pipelines)
        self.library = library or Library()

    def resolved_path(self, identity):
        return self.root/'resolved'/f'{identity}.json'

    def definition(self, name):
        path = self.root/'definitions'/f'{name}.json'
        if not path.exists():
            raise PipelineError(f'No pipeline definition {name}')
        return read_json(path)

    def resolve(self, definition, save=True):
        """Resolve an authored definition; return the resolved pipeline."""
        if 'root' in definition:
            tree = copy.deepcopy(definition['root'])
        elif 'base' in definition:
            tree = apply(self.load(definition['base'])['root'], definition.get('changes', []))
        else:
            raise PipelineError('A definition needs a root tree or a base pipeline ID')
        library, prompts = {}, {}
        problems = []
        for path, node in walk(tree):
            problems += self._check(path or node.get('name', ''), node, library, prompts)
        if problems:
            raise PipelineError('Invalid pipeline:\n  '+'\n  '.join(problems))
        body = {'schema': SCHEMA, 'root': tree, 'library': dict(sorted(library.items())),
                'prompts': dict(sorted(prompts.items()))}
        identity = 'p-'+value_hash(body)[:12]
        resolved = {'id': identity, **body, 'name': definition.get('name'),
                    'description': definition.get('description', ''), 'definition': definition,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'commit': _commit(),
                    'lockfile': _lockfile()}
        if save:
            path = self.resolved_path(identity)
            if not path.exists():
                save_json(path, resolved)
            resolved = read_json(path)
        return resolved

    def _check(self, where, node, library, prompts):
        problems = []
        unknown = set(node) - NODE_KEYS
        if unknown:
            problems.append(f'{where}: unknown keys {", ".join(sorted(unknown))}')
        if not NODE_NAME.fullmatch(str(node.get('name', ''))):
            problems.append(f'{where}: position names use lowercase letters, numbers, dashes and underscores')
        names = [c.get('name') for c in node.get('children', [])]
        if len(names) != len(set(names)):
            problems.append(f'{where}: child names must be unique')
        try:
            name = check_name(node.get('step'))
            cls = self.library.step_class(name)
        except (LibraryError, SyntaxError) as exc:
            return problems+[f'{where}: {exc}']
        library[name] = {'hash': self.library.entry_hash(name), 'interface': cls.interface}
        for helper in self.library.uses(name):
            library[helper] = {'hash': self.library.entry_hash(helper), 'interface': None}
        if cls.interface not in INTERFACES:
            problems.append(f'{where}: {name} targets unsupported interface {cls.interface}')
        bad = set(node.get('settings', {})) - set(cls.defaults)
        if bad:
            problems.append(f'{where}: {name} has no settings {", ".join(sorted(bad))}')
        given = set(node.get('prompts', {}))
        if given != set(cls.prompts):
            problems.append(f'{where}: {name} needs prompts {sorted(cls.prompts)}, got {sorted(given)}')
        for prompt in node.get('prompts', {}).values():
            try:
                prompts[prompt] = self.library.prompt(prompt)[1]
            except LibraryError as exc:
                problems.append(f'{where}: {exc}')
        if cls.children is not None and names != list(cls.children):
            problems.append(f'{where}: {name} expects children {list(cls.children)}, got {names}')
        if hasattr(cls, 'check'):
            problems += [f'{where}: {p}' for p in cls.check(node)]
        return problems

    # Stored pipelines -----------------------------------------------------
    def load(self, ref):
        """A resolved pipeline by ID or tag."""
        identity = ref if ref.startswith('p-') else self.resolve_tag(ref)
        path = self.resolved_path(identity)
        if not path.exists():
            raise PipelineError(f'No resolved pipeline {identity}')
        return read_json(path)

    def all(self):
        return [read_json(p) for p in sorted((self.root/'resolved').glob('p-*.json'))]

    def verify(self, resolved):
        """Problems with a resolved pipeline's library entries and prompts."""
        problems = []
        for name, entry in resolved['library'].items():
            try:
                if self.library.entry_hash(name) != entry['hash']:
                    problems.append(f'{name}: changed after {resolved["id"]} used it')
            except LibraryError as exc:
                problems.append(str(exc))
        for name, digest in resolved['prompts'].items():
            try:
                self.library.prompt(name, digest)
            except LibraryError as exc:
                problems.append(str(exc))
        return problems

    def build(self, resolved, at=''):
        """Instantiate the step tree (or the subtree at a position) from a resolved pipeline."""
        problems = self.verify(resolved)
        if problems:
            raise PipelineError('Cannot build '+resolved['id']+':\n  '+'\n  '.join(problems))
        hashes = resolved['library']

        def make(node):
            cls = self.library.step_class(node['step'], hashes[node['step']]['hash'])
            prompts = {slot: self.library.prompt(name, resolved['prompts'][name])[0]
                       for slot, name in node.get('prompts', {}).items()}
            helpers = {h: self.library.module(h, hashes[h]['hash']) for h in self.library.uses(node['step'])}
            return cls(node, prompts, [make(c) for c in node.get('children', [])], helpers)
        return make(find(resolved['root'], at))

    # Tags -------------------------------------------------------------------
    def tag_log(self):
        path = self.root/'tags.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def tags(self):
        current = {}
        for entry in self.tag_log():
            current[entry['tag']] = entry
        return current

    def resolve_tag(self, tag):
        entry = self.tags().get(tag)
        if entry is None:
            raise PipelineError(f'No tag {tag}')
        return entry['pipeline']

    def set_tag(self, tag, identity, note='', evidence=None):
        if not TAG.fullmatch(tag):
            raise PipelineError('Tags use lowercase letters, numbers, dots, dashes and underscores')
        self.load(identity)
        entry = {'tag': tag, 'pipeline': identity, 'at': datetime.now(timezone.utc).isoformat(),
                 'note': note, 'evidence': evidence or {}, 'commit': _commit()}
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'tags.jsonl').open('a') as f:
            f.write(json.dumps(entry, ensure_ascii=False)+'\n')
        return entry


def diff(a, b):
    """Positions that differ between two resolved pipelines, with what changed."""
    left = {p: n for p, n in walk(a['root'])}
    right = {p: n for p, n in walk(b['root'])}
    changes = []
    for path in sorted(set(left) | set(right)):
        x, y = left.get(path), right.get(path)
        if x is None or y is None:
            changes.append({'path': path, 'change': 'added' if x is None else 'removed',
                            'step': (y or x)['step']})
            continue
        details = {}
        if x['step'] != y['step']:
            details['step'] = [x['step'], y['step']]
        for key in ('settings', 'prompts', 'inputs', 'save'):
            if x.get(key) != y.get(key):
                details[key] = [x.get(key), y.get(key)]
        if [c['name'] for c in x.get('children', [])] != [c['name'] for c in y.get('children', [])]:
            details['children'] = [[c['name'] for c in x.get('children', [])],
                                   [c['name'] for c in y.get('children', [])]]
        if details:
            changes.append({'path': path, 'change': 'changed', **details})
    return changes


def _commit():
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _lockfile():
    path = ROOT/'uv.lock'
    return file_hash(path) if path.exists() else None
