"""Scope triage for one-docket cases: the September questions, about the case's own docket.

When a case shares a document with other dockets' cases (composition
``shared``), the request names the case's docket and says the others are
separate cases. Every other case's request is exactly the September one, so
its saved answer is reused.

Output per case: the validated triage answer, its route, the disposition it
establishes, whether that excludes the case, the conversation for a step that
continues it, and warnings.
"""
import json

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Step, is_failure

TARGET = ('CASE DOCKET\n'
          'This case is docket {docket}. Some of these documents were also issued in other dockets ({others}); '
          'those are separate appeals, analyzed as separate cases. Answer every question about docket {docket}: '
          'its appeal, the municipal decision it challenges and its scope. Documents that name docket {docket} '
          'belong to this case.')


def target_note(case):
    """The note naming a shared case's docket, or None when no document belongs to another docket."""
    if case.get('composition') != 'shared' or len(case.get('dockets') or []) != 1:
        return None
    docket = case['dockets'][0]
    others = sorted({d for doc in case['documents'] for d in doc['caption_dockets'] or doc['listing_dockets']
                     if d != docket and not d.startswith('eo') and '-AP-' not in d})
    return TARGET.format(docket=docket, others=', '.join(others) or 'others') if others else None


class Triage(Step):
    description = ('Reads the whole case text and decides whether the case\'s docket is a municipal appeal about '
                   'housing and which documents belong. A case sharing documents with other dockets is told which '
                   'docket is its own. A "no" on either scope question, or no belonging documents, excludes the case; '
                   'otherwise its disposition is in scope or uncertain.')
    inputs = {'cases': 'cases'}
    prompts = ('system', 'instructions')
    defaults = {'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536}
    uses = ('legacy/v6:validation', 'legacy/v6:conversion')

    def run(self, inputs, ctx):
        cases = {k: v for k, v in inputs['cases'].items() if not is_failure(v)}
        return dict(ctx.map(lambda item: (item[0], self.one(item[0], item[1], ctx)), cases.items()))

    def one(self, key, case, ctx):
        gs, v6 = self.use('legacy/v6:validation'), self.use('legacy/v6:conversion')
        provider = ctx.provider(self.settings)
        pages = case_contract.pages(case['text'])
        note = target_note(case)
        conversation = Conversation.start(provider.name, self.prompt('system'),
                                          case['text']+'\n\n'+(note+'\n\n' if note else '')+self.prompt('instructions'))

        def accept(value):
            triage, warnings = gs.validate_triage(json.dumps(value), pages, version='v5-triage')
            if triage is None:
                raise ValueError(str(warnings))
            return triage, warnings
        answer = ctx.ask([key], self.settings, provider.render(conversation, self.settings), accept)
        if is_failure(answer):
            return answer
        triage, warnings = answer
        route = gs.route_triage(triage, list(pages))
        disposition = v6.scope_disposition(triage, route)
        for warning in warnings:
            ctx.note(key, 'warning', text=warning)
        # A continuing step sees the validated decision, not the raw answer.
        continued = conversation.answer(json.dumps(triage))
        return {'triage': triage, 'route': route, 'disposition': disposition,
                'excluded': disposition['value'] == 'excluded', 'conversation': [list(m) for m in continued.messages]}
