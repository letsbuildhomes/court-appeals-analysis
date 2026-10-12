"""Render each group's documents into one case text, with document and page markers.

Pages without usable text appear as ``[No selected text: STATUS]``, so page
numbering stays complete. The format is the cases contract's; citations
point into it by document and page number.
"""
import hashlib
import html

from court.contracts import pages as page_contract
from court.core.step import Step

DOCUMENT_FIELDS = ('doc', 'sha256', 'title', 'posted', 'listing_dockets', 'caption_dockets', 'basis', 'flags',
                   'pages', 'chars')


def render(documents, pages_by_doc):
    chunks = [f'<group documents="{len(documents)}">']
    for d in documents:
        if d['sha256'] not in pages_by_doc:
            raise ValueError(f'No selected pages for {d["sha256"]}')
        attrs = " ".join(f'{name}="{html.escape(value)}"' for name, value in (
            ("doc", str(d["doc"])), ("basis", d["basis"]),
            ("caption_dockets", "; ".join(d["caption_dockets"])),
            ("listing_dockets", "; ".join(d["listing_dockets"])),
            ("flags", "; ".join(d["flags"]))))
        chunks.append(f"<document {attrs}>")
        for page in pages_by_doc[d['sha256']]:
            body = page['text'] if page['text'] else f"[No selected text: {page['status']}]"
            chunks.append(f'<page number="{page["page"]}" status="{page["status"]}" '
                          f'source="{page["source"] or "none"}">\n{html.escape(body, quote=False)}\n</page>')
        chunks.append("</document>")
    chunks.append("</group>")
    return "\n".join(chunks)


class PageTagged(Step):
    description = 'Joins each group\'s documents, in order, into one text with document and page markers.'
    inputs = {'groups': None, 'pages': 'pages'}
    output = 'cases'

    def run(self, inputs, ctx):
        by_doc = page_contract.by_document(inputs['pages'])
        cases = {}
        for group_id, group in inputs['groups'].items():
            documents = [{'doc': i, **{k: d[k] for k in DOCUMENT_FIELDS if k != 'doc'}}
                         for i, d in enumerate(group['documents'], 1)]
            text = render(documents, by_doc)
            cases[group_id] = {'case': group_id, 'dockets': group['dockets'], 'composition': group['composition'],
                               'documents': documents, 'text': text,
                               'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        return cases
