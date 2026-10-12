"""The whole pipeline on synthetic PDFs: fetch, text, cases and extract, with a fake model.

A saved listing page links two one-page PDFs. Fetching reads the saved
listing and finds the downloads in the cache; text comes from Poppler; the
fake model answers the September prompts. The results must match the
checked-in map, and every dataset ID must be the same on a second run.
"""
import json
from pathlib import Path
import hashlib
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from court.config import ROOT
from court.core.context import Cache
from court.core.datasets import Datasets
from court.core.library import Library
from court.core.models import CallStore
from court.core.pipelines import Pipelines
from court.core.raw import Raw
from court.core.runner import Runner

EXPECTED = Path(__file__).with_name('fixtures')/'end_to_end'/'results.json'
TEXTS = {
    'appeal': ('In re Maple Street Subdivision Docket No. 12-3-20 Vtec Decision on the Merits. The Hartford '
               'Development Review Board approved twelve dwelling units and the neighbors appealed.'),
    'gravel': ('In re Route 7 Gravel Pit Docket No. 7-1-21 Vtec Decision on the Merits. The Act 250 District '
               'Commission permitted a gravel extraction operation and the neighbors appealed.'),
}


def one_page_pdf(text):
    stream = f'BT /F1 10 Tf 40 700 Td ({text}) Tj ET'.encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1400 792] /Resources << /Font << /F1 5 0 R >> >> '
               b'/Contents 4 0 R >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    output, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def listing(entries):
    rows = ''.join(f'''<article class="views-row media-document">
      <div class="views-field-name"><a href="/media/{n}">{title}</a></div>
      <div class="views-field-field-document-expiration"><time datetime="2021-0{n}-01T12:00:00Z">0{n}/01/2021</time></div>
      <div class="views-field-field-document-court-division"><div class="field-content">Environmental</div></div>
      <div class="views-field-field-document-number"><div class="field-content">{docket}</div></div>
    </article>''' for n, (title, docket) in enumerate(entries, 1))
    return f'<html><body>{rows}</body></html>'.encode()


CITE = None


class FakeModel:
    """Answers the September prompts: triage, draft, batched review and duration."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        messages = request['messages']
        system, last = messages[0]['content'], messages[-1]['content']
        if 'BATCH EDITING' in system and len(messages) == 2:
            cases = json.loads(last)['cases']
            body = {'reviews': [{'group': c['group'], 'review': {'schema_version': 'v6-review-1', 'changes': [],
                                 'flags': [], 'scope': {'verdict': 'agree', 'reason': '', 'evidence': []}}}
                                for c in cases]}
        elif 'BATCH EDITING' in system:
            cases = json.loads(last)['cases']
            body = {'groups': [{'group': c['group'], 'dockets': [{
                'docket': c['candidate']['dockets'][0]['docket'], 'note': 'Filed; end unknown', 'end': None,
                'start': {'date': '2020-03-02', 'relation': 'on_or_before', 'description': 'Appeal filed',
                          'evidence': [{'doc': 1, 'page': 1, 'quote': 'the neighbors appealed'}]}}]} for c in cases]}
        elif len(messages) == 2:
            gravel = 'Gravel' in last
            body = {'schema_version': 'v5-triage', 'matter': 'm', 'reason': 'From the caption and decision',
                    'documents': [{'doc': 1, 'caption_dockets': [], 'belongs': 'yes'}],
                    'municipal_appeal': 'no' if gravel else 'yes', 'housing': 'no' if gravel else 'yes',
                    'evidence': [{'doc': 1, 'page': 1, 'quote': 'the neighbors appealed'}]}
        else:
            cite = [{'doc': 1, 'page': 1, 'quote': 'approved twelve dwelling units'}]
            body = {'schema_version': 'v6', 'dockets': [{
                'docket': '12-3-20 Vtec', 'related_dockets': [],
                'project': {'description': 'Twelve dwelling units on Maple Street', 'municipality': 'Hartford',
                            'proposed_units': 12, 'proposed_lots': None, 'evidence': cite},
                'municipal_decision': {'description': 'Approval', 'date': None, 'outcome': 'approved', 'evidence': cite},
                'appellants': [{'name': 'The neighbors', 'relationship': 'neighbor', 'seeks': 'overturn_approval',
                                'representation': 'unclear', 'evidence': [{'doc': 1, 'page': 1,
                                                                          'quote': 'the neighbors appealed'}]}],
                'discretion': {'involved': 'unclear', 'description': None, 'evidence': [], 'central': 'unclear',
                               'central_evidence': []},
                'timeline': [], 'observed_status': {'litigation': 'unclear', 'permission': 'unclear',
                                                    'description': None, 'evidence': []},
                'restrictions': [], 'housing_changes': []}]}
        response = Mock(status_code=200, text=json.dumps(body))
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(body)}}],
                                      'usage': {'prompt_tokens': 1000, 'completion_tokens': 100}}
        return response


@unittest.skipUnless(all(shutil.which(t) for t in ('pdfinfo', 'pdftotext', 'pdfimages', 'pdftoppm')),
                     'Poppler tools required')
class EndToEndTests(unittest.TestCase):
    def test_synthetic_pdfs_through_every_stage(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, cache = Raw(root/'in'), Cache(root/'cache')
            raw.save_listing_page('fixture', 0, listing([('Maple Street Subdivision', '12-3-20 Vtec'),
                                                         ('Route 7 Gravel Pit', '7-1-21 Vtec')]))
            for n, text in enumerate(TEXTS.values(), 1):
                pdf = one_page_pdf(text)
                sha = hashlib.sha256(pdf).hexdigest()
                (root/'in/pdfs').mkdir(parents=True, exist_ok=True)
                (root/'in/pdfs'/f'{sha}.pdf').write_bytes(pdf)
                cache.put({'entry': 'fetch:vtec-environmental', 'media_url': f'https://www.vtcourts.gov/media/{n}'},
                          {'sha256': sha, 'bytes': len(pdf), 'final_url': f'https://www.vtcourts.gov/media/{n}',
                           'content_type': 'application/pdf', 'etag': None, 'last_modified': None,
                           'file': f'in/pdfs/{sha}.pdf'})
            (root/'models.json').write_text((ROOT/'pipelines/models.json').read_text())
            definition = json.loads((ROOT/'pipelines/definitions/september.json').read_text())
            definition['root']['children'][0]['settings']['snapshot'] = 'fixture'
            pipelines = Pipelines(root/'pipelines', Library())
            pipeline = pipelines.resolve(definition)
            datasets = Datasets(root/'datasets')
            runner = Runner(pipelines, datasets, root/'runs', CallStore(root/'calls'), cache, raw)
            model = FakeModel()
            first = runner.run('first', pipeline['id'], {}, mode='send', limit=1.0, transport=model, workers=1)
            again = runner.run('again', pipeline['id'], {}, mode='cached', workers=1)
            self.assertEqual(first['outputs'], again['outputs'])
            self.assertEqual(len(model.requests), 5)  # two triage calls, then one each for draft, review and duration
            results = datasets.open(first['outputs']['extract']).load()
            cases = datasets.open(first['outputs']['cases']).load()
            self.assertEqual(sorted(cases), ['g-12-3-20-vtec', 'g-7-1-21-vtec'])
            expected = json.loads(EXPECTED.read_text())
            self.assertEqual(json.loads(json.dumps(results)), expected)


if __name__ == '__main__':
    unittest.main()
