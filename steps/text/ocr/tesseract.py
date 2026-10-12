"""OCR the pages the usability check sent to OCR, with Tesseract.

Each page is rendered at ``long_edge`` pixels in grayscale and read with
Tesseract (``lang``, ``psm``). Results are cached per page and settings, so a
page is read once; the Tesseract and Poppler versions are recorded with each
result rather than in its key.

Output: "<PDF hash>/<page>" → {doc, page, status, text, alnum, words, bad,
mean_confidence, error}, only for pages that needed OCR.
"""
from pathlib import Path
import tempfile

from court.core.step import Step, is_failure


def tesseract_text(tsv):
    """Line text and word confidences from Tesseract's level-5 TSV rows."""
    lines, scores = {}, []
    # Tesseract does not escape quotes in its final column; split at the first 11 tabs.
    for line in tsv.splitlines()[1:]:
        fields = line.split('\t', 11)
        if len(fields) != 12 or fields[0] != '5' or not (word := fields[11].strip()):
            continue
        try:
            confidence = float(fields[10])
            [int(value) for value in fields[6:10]]
            line_key = tuple(int(value) for value in fields[1:5])
        except ValueError:
            continue
        if not 0 <= confidence <= 100:
            continue
        lines.setdefault(line_key, []).append(word)
        scores.append(confidence / 100)
    return '\n'.join(' '.join(parts) for parts in lines.values()), scores


def mean_confidence(scores):
    values = [value * 100 for value in scores if 0 <= value <= 1]
    return sum(values) / len(values) if values else None


class Tesseract(Step):
    description = 'Reads pages without usable native text with Tesseract OCR.'
    inputs = {'documents': 'documents', 'usability': None}
    defaults = {'lang': 'eng', 'psm': 3, 'long_edge': 2200}
    uses = ('text/shared:tools',)

    def run(self, inputs, ctx):
        tools = self.use('text/shared:tools')
        s = self.settings
        wanted = sorted(k for k, v in inputs['usability'].items()
                        if not is_failure(v) and v['decision'] in ('ocr_needed', 'ocr_candidate'))

        def read(doc, page):
            with tempfile.TemporaryDirectory(prefix='court-ocr-') as folder:
                prefix = Path(folder)/f'page_{page:05d}'
                tools.command(['pdftoppm', '-f', str(page), '-l', str(page), '-scale-to', str(s['long_edge']),
                               '-gray', '-singlefile', '-png', str(ctx.raw.pdf(doc)), str(prefix)])
                image = prefix.with_suffix('.png')
                if not image.is_file():
                    raise RuntimeError(f'pdftoppm did not produce page {page}')
                tsv = tools.command(['tesseract', str(image), 'stdout', '-l', s['lang'], '--psm', str(s['psm']),
                                     'tsv']).stdout.decode('utf-8', errors='replace')
            text, scores = tesseract_text(tsv)
            alnum, words, bad = tools.metrics(text)
            return {'tool': tools.version('tesseract', '--version')+'|'+tools.version('pdftoppm'),
                    'status': 'complete', 'text': text, 'alnum': alnum, 'words': words, 'bad': bad,
                    'mean_confidence': mean_confidence(scores), 'error': None}

        def one(key):
            doc, page = inputs['usability'][key]['doc'], inputs['usability'][key]['page']
            parts = {'doc': doc, 'page': page, 'settings': dict(sorted(s.items()))}
            try:
                result = ctx.cached(parts, lambda: read(doc, page))
            except Exception as exc:
                result = {'status': 'failed', 'text': None, 'alnum': None, 'words': None, 'bad': None,
                          'mean_confidence': None, 'error': str(exc)}
            return key, {'doc': doc, 'page': page, **{k: v for k, v in result.items() if k != 'tool'}}
        return dict(ctx.map(one, wanted))
