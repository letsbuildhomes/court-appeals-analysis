"""Native text of every PDF page, from Poppler's pdftotext in layout mode.

Each document's result is cached by its hash, so a PDF is extracted once.
Output: "<PDF hash>/<page>" → {doc, page, text, alnum, words, bad}. A document
that cannot be read is one failure under its first page.
"""
import re

from court.core.step import Failure, Step



def split_pages(raw, expected):
    parts = raw.decode('utf-8', errors='replace').split('\f')
    if len(parts) == expected + 1 and not parts[-1].strip():
        parts.pop()
    if len(parts) != expected:
        raise ValueError(f'pdftotext returned {len(parts)} pages; expected {expected}')
    return parts


class PdftotextLayout(Step):
    description = 'Extracts each page\'s embedded text with pdftotext -layout.'
    inputs = {'documents': 'documents'}
    uses = ('text/shared:tools',)

    def run(self, inputs, ctx):
        tools = self.use('text/shared:tools')

        def extract(sha):
            path = ctx.raw.pdf(sha)
            info = tools.command(['pdfinfo', str(path)]).stdout.decode('utf-8', errors='replace')
            match = re.search(r'^Pages:\s*(\d+)\s*$', info, re.MULTILINE)
            if not match or int(match.group(1)) < 1:
                raise ValueError(f'Could not find a positive page count in pdfinfo: {path}')
            raw = tools.command(['pdftotext', '-enc', 'UTF-8', '-layout', str(path), '-']).stdout
            return {'tool': tools.version('pdfinfo'), 'pages': split_pages(raw, int(match.group(1)))}

        def one(sha):
            try:
                return sha, ctx.cached({'doc': sha}, lambda: extract(sha)), None
            except Exception as exc:
                return sha, None, exc
        output = {}
        for sha, result, error in ctx.map(one, sorted(inputs['documents'])):
            if error is not None:
                output[f'{sha}/1'] = Failure('native_text', error, ctx.path)
                continue
            for number, text in enumerate(result['pages'], 1):
                alnum, words, bad = tools.metrics(text)
                output[f'{sha}/{number}'] = {'doc': sha, 'page': number, 'text': text, 'alnum': alnum,
                                             'words': words, 'bad': bad}
        return output
