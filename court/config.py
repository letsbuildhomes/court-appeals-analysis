"""Paths and settings shared by the framework and tools.

Every writable location is derived from ``Paths``, so tests and practice runs
can point the whole system at another directory.
"""
from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    data: Path = ROOT/'data'

    @property
    def steps(self):
        return self.root/'steps'

    @property
    def prompts(self):
        return self.root/'prompts'

    @property
    def pipelines(self):
        return self.root/'pipelines'

    @property
    def experiments(self):
        return self.root/'experiments'

    @property
    def reference(self):
        return self.root/'reference'

    @property
    def datasets(self):
        return self.data/'datasets'

    @property
    def blobs(self):
        return self.root/'in'/'pdfs'

    @property
    def calls(self):
        return self.data/'calls'

    @property
    def cache(self):
        return self.data/'cache'

    @property
    def runs(self):
        return self.data/'runs'

    @property
    def models(self):
        return self.root/'pipelines'/'models.json'


def paths():
    """The active paths; ``COURT_DATA`` moves the data directory (for practice and tests)."""
    data = os.getenv('COURT_DATA')
    return Paths(data=Path(data)) if data else Paths()
