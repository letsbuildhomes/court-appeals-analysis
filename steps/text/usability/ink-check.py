"""Decide which pages' native text is usable and which need OCR.

A page with enough clean native text is ``native_ok``. A sparse or garbled
page is rendered to measure its ink: nearly no ink makes it ``blank``;
visible content without enough text makes it ``ocr_needed``; anything
uncertain is an ``ocr_candidate``. Each document's decisions are cached.

Output: "<PDF hash>/<page>" → {doc, page, decision, reason, ink, images, large_image, render_error}.
"""
from pathlib import Path
import tempfile
from collections import defaultdict

from court.core.hashing import value_hash
from court.core.step import Failure, Step, is_failure


def pgm_ink_ratio(data):
    """Share of dark pixels in Poppler's 8-bit grayscale PGM output."""
    position = 0

    def token():
        nonlocal position
        while position < len(data):
            if data[position] in b' \t\r\n':
                position += 1
            elif data[position] == ord('#'):
                end = data.find(b'\n', position)
                position = len(data) if end < 0 else end + 1
            else:
                break
        start = position
        while position < len(data) and data[position] not in b' \t\r\n':
            position += 1
        return data[start:position]

    magic, width, height, maximum = token(), int(token()), int(token()), int(token())
    if magic != b'P5' or maximum != 255 or width < 1 or height < 1:
        raise ValueError('Unexpected PGM format from pdftoppm')
    if position >= len(data) or data[position] not in b' \t\r\n':
        raise ValueError('Missing PGM header terminator')
    position += 2 if data[position:position + 2] == b'\r\n' else 1
    pixels = data[position:]
    if len(pixels) != width * height:
        raise ValueError(f'PGM pixel count {len(pixels)} != {width * height}')
    return sum(pixel < 245 for pixel in pixels) / len(pixels)


def decide(alnum, bad, ink, image_sizes, render_error, clear, short, blank):
    if bad > max(5, alnum // 100):
        return 'ocr_candidate', 'suspicious_native_characters'
    if alnum >= clear:
        return 'native_ok', 'substantial_native_text'
    full_page_image = any(min(width, height) >= 1000 for width, height in image_sizes)
    if alnum >= short and not full_page_image:
        return 'native_ok', 'short_native_text_without_large_image'
    if render_error or ink is None:
        return 'ocr_candidate', 'could_not_render_sparse_page'
    if alnum == 0 and ink < blank:
        return 'blank', 'no_text_or_visible_marks'
    if alnum == 0 or full_page_image:
        return 'ocr_needed', 'visible_content_with_insufficient_native_text'
    return 'ocr_candidate', 'short_text_with_visible_content'


class InkCheck(Step):
    description = ('Keeps native text where there is enough of it; renders sparse pages to tell blank pages '
                   'from pages that need OCR.')
    inputs = {'documents': 'documents', 'native': None}
    defaults = {'clear_text': 80, 'short_text': 20, 'blank_ink': 0.0005, 'render_scale': 512}
    uses = ('text/shared:tools',)

    def run(self, inputs, ctx):
        tools = self.use('text/shared:tools')
        s = self.settings
        by_doc = defaultdict(list)
        for key, page in inputs['native'].items():
            if not is_failure(page):
                by_doc[page['doc']].append(page)

        def images(path):
            output = tools.command(['pdfimages', '-list', str(path)]).stdout.decode('utf-8', errors='replace')
            found = defaultdict(list)
            for line in output.splitlines()[2:]:
                fields = line.split()
                if len(fields) >= 5 and fields[0].isdigit() and fields[3].isdigit() and fields[4].isdigit():
                    found[int(fields[0])].append((int(fields[3]), int(fields[4])))
            return found

        def ink(path, number, folder):
            prefix = Path(folder)/f'page_{number:05d}'
            tools.command(['pdftoppm', '-f', str(number), '-l', str(number), '-scale-to', str(s['render_scale']),
                           '-gray', '-singlefile', str(path), str(prefix)])
            image = prefix.with_suffix('.pgm')
            try:
                return pgm_ink_ratio(image.read_bytes())
            finally:
                image.unlink(missing_ok=True)

        def check(sha, pages):
            path = ctx.raw.pdf(sha)
            sparse = any(p['alnum'] < s['clear_text'] or p['bad'] > max(5, p['alnum'] // 100) for p in pages)
            image_error, sizes_by_page = None, {}
            try:
                sizes_by_page = images(path) if sparse else {}
            except (OSError, RuntimeError, ValueError) as exc:
                image_error = str(exc)
            decisions = []
            with tempfile.TemporaryDirectory(prefix='court-usability-') as folder:
                for p in pages:
                    sizes, measured, render_error = sizes_by_page.get(p['page'], []), None, None
                    if p['alnum'] < s['clear_text'] or p['bad'] > max(5, p['alnum'] // 100):
                        try:
                            measured = ink(path, p['page'], folder)
                        except (OSError, RuntimeError, ValueError) as exc:
                            render_error = str(exc)
                    decision, reason = decide(p['alnum'], p['bad'], measured, sizes, render_error,
                                              s['clear_text'], s['short_text'], s['blank_ink'])
                    if image_error and p['alnum'] < s['clear_text']:
                        decision, reason, render_error = 'ocr_candidate', 'could_not_inspect_images', image_error
                    decisions.append({'decision': decision, 'reason': reason, 'ink': measured, 'images': len(sizes),
                                      'large_image': any(min(w, h) >= 1000 for w, h in sizes),
                                      'render_error': render_error})
            return {'tool': tools.version('pdfinfo'), 'pages': decisions}

        def one(item):
            sha, pages = item
            pages = sorted(pages, key=lambda p: p['page'])
            parts = {'doc': sha, 'native': value_hash([p['text'] for p in pages]),
                     'settings': dict(sorted(s.items()))}
            try:
                return sha, pages, ctx.cached(parts, lambda: check(sha, pages)), None
            except Exception as exc:
                return sha, pages, None, exc
        output = {k: v for k, v in inputs['native'].items() if is_failure(v)}
        for sha, pages, result, error in ctx.map(one, sorted(by_doc.items())):
            for p, decision in zip(pages, result['pages'] if result else [None]*len(pages)):
                key = f'{sha}/{p["page"]}'
                output[key] = Failure('usability', error, ctx.path) if error else \
                    {'doc': sha, 'page': p['page'], **decision}
        return output
