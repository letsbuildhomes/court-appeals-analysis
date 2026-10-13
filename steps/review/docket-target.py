"""Editorial review for one-docket cases: the September review, told a shared case's docket.

A case that shares a document with other dockets' cases carries a
``case_docket`` note in its batch entry saying which docket the draft is
for; the others are separate cases. Batches of other cases are sent exactly
as in September.

Cases are packed in case order into batches of up to ``batch_cases`` cases
whose text plus draft stays within ``batch_chars`` characters. A research
editor checks each draft against its source and returns a patch, or flags a
doubtful scope decision, which leaves the case uncertain for human review.

Output per case: the review, the corrected draft (or the original, when
flagged), whether it was flagged and the flagged disposition, the batch's
members, and the batch conversation for a step that continues it.
"""
import json

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Failure, Step, is_failure

BATCH_EDITING = '''Review this batch as one research editor. Apply the same definitions consistently,
but never transfer facts, dates, people or citations between groups. Doc/page numbering
is local to each group. Return JSON {"reviews":[{"group":"exact group id","review":{...}}]}.
Include every supplied group exactly once, with a complete v6-review-1 patch.'''


def case_docket(case):
    """The note naming a shared case's docket, or None for a case whose documents are all its own."""
    if case.get('composition') != 'shared' or len(case.get('dockets') or []) != 1:
        return None
    return (f'This case is docket {case["dockets"][0]}. Some source documents were also issued in other dockets, '
            'which are separate cases; review the candidate as the record for this docket only.')


def batches(keys, size, max_cases, max_chars):
    """Pack keys in order up to a case count and a character budget."""
    result, batch, total = [], [], 0
    for key in keys:
        amount = size(key)
        if batch and (len(batch) >= max_cases or total+amount > max_chars):
            result.append(batch)
            batch, total = [], 0
        batch.append(key)
        total += amount
    if batch:
        result.append(batch)
    return result


class Review(Step):
    description = ('A research editor checks each draft against its source and corrects fields and citations, '
                   'or flags a doubtful scope decision, which leaves the case uncertain for human review. A case '
                   'sharing documents with other dockets is told which docket is its own. Its system prompt joins '
                   'the primer with the extraction, scope and editing contracts.')
    inputs = {'cases': 'cases', 'triage': None, 'drafts': None}
    prompts = ('primer', 'extract', 'triage', 'review')
    defaults = {'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536,
                'batch_cases': 2, 'batch_chars': 200000}
    uses = ('legacy/v6:validation', 'legacy/v6:conversion', 'legacy/v6:review-patch')

    def run(self, inputs, ctx):
        cases, triage, drafts = inputs['cases'], inputs['triage'], inputs['drafts']
        keys = sorted(k for k in drafts if not is_failure(drafts[k]))
        size = (lambda k: len(cases[k]['text']) + len(json.dumps(drafts[k]['draft'])))
        groups = batches(keys, size, self.settings['batch_cases'], self.settings['batch_chars'])
        output = {}
        for result in ctx.map(lambda members: self.batch(members, cases, triage, drafts, ctx), groups):
            output.update(result)
        return output

    def batch(self, members, cases, triage, drafts, ctx):
        v6, patch = self.use('legacy/v6:conversion'), self.use('legacy/v6:review-patch')
        provider = ctx.provider(self.settings)
        p = {slot: self.prompt(slot) for slot in self.prompts}
        system = (p['primer']+'\n\nEXTRACTION CONTRACT\n'+p['extract']+'\n\nSCOPE CONTRACT\n'+p['triage']
                  + '\n\nEDITING CONTRACT\n'+p['review']+'\n\nBATCH EDITING\n'+BATCH_EDITING)
        data = {'cases': [{'group': k, **({'case_docket': note} if (note := case_docket(cases[k])) else {}),
                           'source_documents': cases[k]['text'], 'triage': triage[k]['triage'],
                           'candidate': drafts[k]['draft']} for k in members]}
        conversation = Conversation.start(provider.name, system, json.dumps(data, ensure_ascii=False))

        def accept(value):
            v6.exact_members(value['reviews'], 'group', members)
            return value
        value = ctx.ask(members, self.settings, provider.render(conversation, self.settings), accept)
        if is_failure(value):
            return {k: value for k in members}
        reviews = {r['group']: r['review'] for r in value['reviews']}
        continued = [list(m) for m in conversation.answer(json.dumps(value, ensure_ascii=False)).messages]
        output = {}
        for key in members:
            try:
                review = reviews[key]
                candidate, warnings = patch.apply_review(drafts[key]['draft'], review,
                                                         case_contract.pages(cases[key]['text']))
            except Exception as exc:
                output[key] = Failure('validation', exc, ctx.path)
                ctx.note(key, 'rejected', reason=str(exc))
                continue
            for warning in warnings:
                ctx.note(key, 'warning', text=warning)
            flagged = review['scope']['verdict'] == 'flag'
            output[key] = {'review': review, 'flagged': flagged,
                           'draft': drafts[key]['draft'] if flagged else candidate,
                           'disposition': v6.flagged_disposition(triage[key]['disposition'], review) if flagged
                           else triage[key]['disposition'],
                           'batch': members, 'conversation': continued}
        return output
