"""The raw input store: source files exactly as fetched, never changed.

    in/pdfs/<sha256>.pdf                     PDFs, named by content hash
    in/listings/<snapshot>/page_NNNN.html    listing pages, as fetched, by snapshot

Steps reach it as ``ctx.raw``. Files are written once, atomically.
"""
import hashlib
import os
from pathlib import Path
import tempfile

from court.config import paths


class Raw:
    def __init__(self, root=None):
        self.root = Path(root or paths().root/'in')

    def pdf(self, sha256):
        return self.root/'pdfs'/f'{sha256}.pdf'

    def relative_pdf(self, sha256):
        return f'in/pdfs/{sha256}.pdf'

    def has_pdf(self, sha256):
        path = self.pdf(sha256)
        if not path.exists():
            return False
        digest = hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest() == sha256

    def temporary(self):
        """A temporary file beside the PDFs, to stream a download into."""
        (self.root/'pdfs').mkdir(parents=True, exist_ok=True)
        return tempfile.NamedTemporaryFile(mode='wb', dir=self.root/'pdfs', prefix='download-', suffix='.part',
                                           delete=False)

    def keep_pdf(self, temporary, sha256):
        """Move a fully written temporary file into place under its hash."""
        if self.has_pdf(sha256):
            Path(temporary).unlink()
        else:
            os.replace(temporary, self.pdf(sha256))
        return self.relative_pdf(sha256)

    def listing_page(self, snapshot, index):
        path = self.root/'listings'/snapshot/f'page_{index:04d}.html'
        return path.read_bytes() if path.exists() else None

    def save_listing_page(self, snapshot, index, html):
        path = self.root/'listings'/snapshot/f'page_{index:04d}.html'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.html.part')
        tmp.write_bytes(html)
        os.replace(tmp, path)
