"""Record extraction for one-docket cases: the September draft, continuing triage's
conversation, keeping only the case's own docket.

A case's records are for its docket alone; records the model drafts for other
dockets the documents mention are dropped, with a warning, since those dockets
are separate cases. When no record matches, the draft is kept as it is, with
a warning: its docket may be written in a way that can't be matched. A case
sharing a document with other
dockets is told again, in the request, which docket to draft; every other
case's request is exactly the September one.
"""
import copy
import json
import re

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Step, is_failure


ONLY = ('\n\nDraft the record for docket {docket} only. The other dockets in these documents are separate '
        'cases; mention them in related_dockets when they bear on this appeal.')


def docket_identity(text):
    """A docket number's digits and court suffix, for matching however it was written."""
    if not isinstance(text, str):
        return None
    m = re.search(r'(\d{1,3})\s*-\s*(\d{1,2})\s*-\s*(\d{2})\s*V|(\d{2})\s*-?\s*(ENV|CC|EC|AP)\s*-?\s*(\d{1,6})',
                  text.replace('\u2010', '-'), re.I)
    if not m:
        return None
    return f'{int(m[1])}-{int(m[2])}-{m[3]}' if m[1] else f'{m[4]}-{m[5].upper()}-{int(m[6])}'


def own_records(draft, dockets):
    """The draft keeping only records for the case's dockets, and the dockets it dropped.

    The dropped list is None when no record matches, and the draft is returned unchanged.
    """
    wanted = {docket_identity(d) for d in dockets}
    records = draft.get('dockets') or []
    if not wanted or not records:
        return draft, []
    kept = [r for r in records if docket_identity(r.get('docket')) in wanted]
    if not kept:
        return draft, None
    dropped = [r.get('docket') for r in records if r not in kept]
    return {**draft, 'dockets': kept}, dropped


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
    description = ('Drafts the record for the case\'s docket: project, decision, appellants, discretion, timeline, '
                   'observed status, restrictions and housing changes, each with cited evidence. Continuing triage\'s '
                   'conversation, it sees the validated scope decision. Records for other dockets are dropped.')
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
        shared = case.get('composition') == 'shared' and len(case.get('dockets') or []) == 1
        conversation = conversation.ask(self.prompt('instructions')
                                        + (ONLY.format(docket=case['dockets'][0]) if shared else ''))

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
        draft, dropped = own_records(draft, case.get('dockets') or [])
        if dropped is None:
            warnings = [*warnings, 'No record is for the case\'s docket; kept as drafted']
        elif dropped:
            warnings = [*warnings, 'Dropped records for other dockets: '+', '.join(str(d) for d in dropped)]
        for warning in warnings:
            ctx.note(key, 'warning', text=warning)
        return {'draft': draft}
