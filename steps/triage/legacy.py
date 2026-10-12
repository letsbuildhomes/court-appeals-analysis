"""Scope triage, as the September run asked it: is this a municipal appeal about housing,
and which documents belong.

Output per case: the validated triage answer, its route, the disposition it
establishes, whether that excludes the case, the conversation for a step that
continues it, and warnings.
"""
import json

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Step, is_failure


class Triage(Step):
    description = ('Reads the whole case text and decides whether it is a municipal appeal about housing and which '
                   'documents belong. A "no" on either scope question, or no belonging documents, excludes the case; '
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
        conversation = Conversation.start(provider.name, self.prompt('system'),
                                          case['text']+'\n\n'+self.prompt('instructions'))

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
