"""The frozen, page-addressable source text every pipeline reads.

The corpus is the production run's sources and case metadata, read in place
and never modified. Each case's text is verified against its recorded hash
before use.
"""
import hashlib
import json
from pathlib import Path

from config import ROOT
from pipeline.cases import Case

PRODUCTION = ROOT/'data/production-import-UazDoN/data/appeals/full-run'


class Corpus:
    def __init__(self, folder=None):
        self.folder = Path(folder or PRODUCTION)
        manifest = json.loads((self.folder/'manifest.json').read_text())
        self.entries = {g['group_id']: {k: g[k] for k in ('group_id', 'dockets', 'documents', 'source_sha256')
                                        if k in g} for g in manifest['groups']}
        self.order = [g['group_id'] for g in manifest['groups']]

    def ids(self):
        return list(self.order)

    def source_path(self, case_id):
        return self.folder/'sources'/f'{case_id}.txt'

    def text(self, case_id):
        if case_id not in self.entries:
            raise KeyError(f'Case {case_id} is not in the corpus')
        text = self.source_path(case_id).read_text()
        if hashlib.sha256(text.encode()).hexdigest() != self.entries[case_id]['source_sha256']:
            raise ValueError(f'Frozen source changed: {case_id}')
        return text

    def cases(self, ids=None):
        """Fresh case states in corpus order."""
        wanted = self.order if ids is None else [i for i in self.order if i in set(ids)]
        missing = set(ids or ()) - set(self.order)
        if missing:
            raise KeyError('Cases absent from the corpus: '+', '.join(sorted(missing)))
        return [Case(i, self.text(i), self.entries[i]) for i in wanted]
