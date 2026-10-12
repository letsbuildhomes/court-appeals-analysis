"""Record extraction, as the September run asked it: one draft v6 record per municipal
appeal docket, continuing triage's conversation."""
import copy
import json

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Step, is_failure


def normalize_extract(value):
    """Relocate fields only when ownership and destination are unambiguous."""
    value = copy.deepcopy(value)
    notes = []
    dockets = value.get('dockets') if isinstance(value, dict) else None
    if not isinstance(dockets, list) or not all(isinstance(d, dict) for d in dockets):
        return value, notes

    def move(source, destination, field, origin, target):
        if field in source and field not in destination:
            destination[field] = source.pop(field)
            notes.append(f'Moved {origin}.{field} to {target}.{field}')
    if len(dockets) == 1:
        for field in ('timeline', 'observed_status', 'restrictions', 'housing_changes'):
            move(value, dockets[0], field, 'extract', 'extract.dockets[0]')
    for index, docket in enumerate(dockets):
        discretion = docket.get('discretion')
        if not isinstance(discretion, dict):
            continue
        target = f'extract.dockets[{index}].discretion'
        for field in ('involved', 'central', 'central_evidence'):
            move(docket, discretion, field, f'extract.dockets[{index}]', target)
            if len(dockets) == 1:
                move(value, discretion, field, 'extract', target)
    return value, notes


class Draft(Step):
    description = ('Drafts one record per municipal appeal docket: project, decision, appellants, discretion, '
                   'timeline, observed status, restrictions and housing changes, each with cited evidence. '
                   'Continuing triage\'s conversation, it sees the validated scope decision.')
    inputs = {'cases': 'cases', 'triage': None}
    prompts = ('system', 'instructions')
    defaults = {'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536, 'continue': True}
    uses = ('legacy/v6:validation',)

    def run(self, inputs, ctx):
        triage = inputs['triage']
        work = [(k, c, triage[k]) for k, c in inputs['cases'].items() if not is_failure(c) and not is_failure(triage[k])]
        return dict(ctx.map(lambda item: (item[0], self.one(*item, ctx)), work))

    def one(self, key, case, triage, ctx):
        gs = self.use('legacy/v6:validation')
        provider = ctx.provider(self.settings)
        pages = case_contract.pages(case['text'])
        if self.settings['continue']:
            conversation = Conversation(provider.name, tuple(tuple(m) for m in triage['conversation']))
        else:
            # A fresh conversation restates the case text and the scope decision.
            conversation = Conversation.start(provider.name, self.prompt('system'),
                                              case['text']+'\n\nSCOPE DECISION\n'+json.dumps(triage['triage']))
        conversation = conversation.ask(self.prompt('instructions'))

        def accept(value):
            candidate, warnings = gs.validate_extract(json.dumps(value), pages, version='v6')
            if candidate is None:
                # A misplaced field may be recoverable without another call.
                normalized, _ = normalize_extract(value)
                candidate, retried = gs.validate_extract(json.dumps(normalized), pages, version='v6')
                if candidate is None:
                    raise ValueError(str(warnings))
                warnings = retried
            candidate.pop('scouting', None)
            return candidate, warnings
        answer = ctx.ask([key], self.settings, provider.render(conversation, self.settings), accept)
        if is_failure(answer):
            return answer
        draft, warnings = answer
        for warning in warnings:
            ctx.note(key, 'warning', text=warning)
        return {'draft': draft}
