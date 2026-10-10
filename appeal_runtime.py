"""Atomic JSON files and hashes shared by the pipeline and legacy run tools.

Model calls, spending caps and reuse live in ``pipeline/store.py``.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write('\n')
        tmp = f.name
    os.replace(tmp, path)


def read(path):
    return json.loads(Path(path).read_text())


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()
