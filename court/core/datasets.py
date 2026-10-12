"""Saved step outputs: immutable keyed datasets identified by their contents.

A dataset is a directory under ``data/datasets/<id>/``:

    manifest.json   kind (its contract), contract version, ID, the key → item hash
                    index, and the producer: pipeline, position, run and input datasets
    items/<key>.json one file per item; a key's slashes become subdirectories

An item is ``{"value": ...}`` or ``{"failure": {...}}``. Its hash is the hash of
that canonical JSON. The dataset ID is ``d-`` plus the hash of the sorted
``key, item hash`` pairs and the kind, so identical contents always have the
same ID, whatever produced them. The producer is recorded beside the ID, not
in it.

A dataset is written as a draft, which may be resumed, and becomes immutable
when finished. Finishing a draft whose contents match an existing dataset
returns that dataset. Names (``refs.jsonl``) are an append-only log of
readable names for dataset IDs.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from court.config import paths
from court.core.hashing import canonical, read_json, save_json, sha256

KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z')
NAME = re.compile(r'[a-z0-9][a-z0-9._-]*\Z')


class Failure:
    """An item that could not be produced, with the step and reason."""

    def __init__(self, kind, reason, step=''):
        self.kind, self.reason, self.step = kind, str(reason), step

    def to_json(self):
        return {'kind': self.kind, 'reason': self.reason, 'step': self.step}

    @classmethod
    def from_json(cls, value):
        return cls(value['kind'], value['reason'], value.get('step', ''))

    def __eq__(self, other):
        return isinstance(other, Failure) and self.to_json() == other.to_json()

    def __repr__(self):
        return f'Failure({self.kind!r}, {self.reason!r}, {self.step!r})'


def is_failure(value):
    return isinstance(value, Failure)


def item(value):
    return {'failure': value.to_json()} if isinstance(value, Failure) else {'value': value}


def unwrap(stored):
    return Failure.from_json(stored['failure']) if 'failure' in stored else stored['value']


def item_hash(value):
    return sha256(canonical(item(value)))


def dataset_id(kind, index):
    lines = ''.join(f'{key}\t{index[key]}\n' for key in sorted(index))
    return 'd-'+sha256(f'{kind}\n{lines}')[:16]


def check_key(key):
    if not isinstance(key, str) or not KEY.fullmatch(key) or '..' in key:
        raise ValueError(f'Invalid dataset key {key!r}')
    return key


class Dataset:
    """A finished, immutable dataset."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.manifest = read_json(self.folder/'manifest.json')
        self.id, self.kind = self.manifest['id'], self.manifest['kind']
        self.index = self.manifest['items']

    def __repr__(self):
        return f'<Dataset {self.id} {self.kind} {len(self.index)} items>'

    def __len__(self):
        return len(self.index)

    def __contains__(self, key):
        return key in self.index

    def keys(self):
        return sorted(self.index)

    def get(self, key):
        if key not in self.index:
            raise KeyError(f'{key} is not in dataset {self.id}')
        return unwrap(read_json(self.folder/'items'/f'{key}.json'))

    def items(self, keys=None):
        for key in self.keys() if keys is None else keys:
            yield key, self.get(key)

    def load(self, keys=None):
        """The dataset as an in-memory map, optionally limited to some keys."""
        wanted = self.keys() if keys is None else [k for k in self.keys() if k in set(keys)]
        return dict(self.items(wanted))

    def verify(self):
        """Recompute every item hash and the ID; return a list of problems."""
        problems = []
        for key, expected in self.index.items():
            path = self.folder/'items'/f'{key}.json'
            if not path.exists():
                problems.append(f'{key}: missing')
            elif sha256(canonical(read_json(path))) != expected:
                problems.append(f'{key}: changed')
        if dataset_id(self.kind, self.index) != self.id:
            problems.append('ID does not match the index')
        return problems


class Draft:
    """A dataset being written. Items may be added or replaced until it is finished."""

    def __init__(self, store, kind, folder, producer=None):
        self.store, self.kind, self.folder = store, kind, Path(folder)
        self.producer = dict(producer or {})
        self.index = {}
        state = self.folder/'draft.json'
        if state.exists():
            saved = read_json(state)
            if saved['kind'] != kind:
                raise ValueError(f'Draft {self.folder.name} is a {saved["kind"]} draft')
            self.index = saved['items']
            self.producer = {**saved.get('producer', {}), **self.producer}

    def __contains__(self, key):
        return key in self.index

    def put(self, key, value):
        check_key(key)
        stored = item(value)
        save_json(self.folder/'items'/f'{key}.json', stored)
        self.index[key] = sha256(canonical(stored))

    def checkpoint(self):
        save_json(self.folder/'draft.json', {'kind': self.kind, 'items': self.index, 'producer': self.producer})

    def finish(self, **extra):
        """Freeze the draft and return the dataset (an existing one if the contents match)."""
        from court.contracts import version_of
        identity = dataset_id(self.kind, self.index)
        target = self.store.folder(identity)
        if target.exists():
            shutil.rmtree(self.folder, ignore_errors=True)
            return Dataset(target)
        manifest = {'id': identity, 'kind': self.kind, 'contract': version_of(self.kind),
                    'created_at': datetime.now(timezone.utc).isoformat(), 'producer': self.producer,
                    **extra, 'items': dict(sorted(self.index.items()))}
        save_json(self.folder/'manifest.json', manifest)
        (self.folder/'draft.json').unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.folder, target)
        return Dataset(target)


class Datasets:
    """The dataset store under ``data/datasets``."""

    def __init__(self, root=None):
        self.root = Path(root or paths().datasets)

    def folder(self, identity):
        return self.root/identity

    def draft(self, kind, producer=None, name=None):
        """A new draft, or the named draft resumed."""
        name = name or uuid.uuid4().hex
        return Draft(self, kind, self.root/'drafts'/name, producer)

    def write(self, kind, values, producer=None, **extra):
        """Write a whole map as a finished dataset."""
        draft = self.draft(kind, producer)
        for key, value in values.items():
            draft.put(key, value)
        return draft.finish(**extra)

    def open(self, ref):
        """A dataset by ID or name."""
        identity = ref if ref.startswith('d-') else self.resolve(ref)
        folder = self.folder(identity)
        if not (folder/'manifest.json').exists():
            raise KeyError(f'No dataset {ref}')
        return Dataset(folder)

    def all(self):
        return [Dataset(p) for p in sorted(self.root.glob('d-*')) if (p/'manifest.json').exists()]

    # Names -------------------------------------------------------------
    def names(self):
        path = self.root/'refs.jsonl'
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        current = {}
        for entry in entries:
            current[entry['name']] = entry['dataset']
        return current

    def name(self, name, identity, note=''):
        if not NAME.fullmatch(name):
            raise ValueError('Dataset names use lowercase letters, numbers, dots, dashes and underscores')
        self.open(identity)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'refs.jsonl').open('a') as f:
            f.write(json.dumps({'name': name, 'dataset': identity, 'note': note,
                                'at': datetime.now(timezone.utc).isoformat()})+'\n')

    def resolve(self, name):
        found = self.names().get(name)
        if found is None:
            raise KeyError(f'No dataset named {name}')
        return found


def diff(a, b):
    """Item-level comparison of two datasets or ``{key: item hash}`` indexes."""
    left = a.index if isinstance(a, Dataset) else a
    right = b.index if isinstance(b, Dataset) else b
    return {'identical': sorted(k for k in left if k in right and left[k] == right[k]),
            'changed': sorted(k for k in left if k in right and left[k] != right[k]),
            'removed': sorted(k for k in left if k not in right),
            'added': sorted(k for k in right if k not in left)}
