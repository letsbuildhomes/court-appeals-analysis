"""Canonical JSON and the hashes every identity in the system is built from."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def canonical(value):
    """The one byte representation used for hashing: sorted keys, no extra spaces, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha256(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def value_hash(value):
    return sha256(canonical(value))


def file_hash(path):
    return sha256(Path(path).read_bytes())


def save_json(path, value):
    """Write JSON atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, suffix='.tmp') as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write('\n')
        tmp = f.name
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text())
