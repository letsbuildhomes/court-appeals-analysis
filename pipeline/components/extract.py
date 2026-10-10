"""Record extraction: one draft record per municipal appeal docket."""
import copy
import json

from pipeline.components.base import Component, register
from pipeline.conversation import Conversation


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


def recover_extract(value, pages):
    import group_scout as gs
    normalized, notes = normalize_extract(value)
    candidate, errors = gs.validate_extract(json.dumps(normalized), pages, version='v6')
    if candidate is None:
        raise ValueError(str(errors))
    return normalized, notes


@register
class Extract(Component):
    name = 'extract'
    summary = ('Drafts one record per municipal appeal docket: project, decision, appellants, '
               'discretion, timeline, outcome, restrictions and housing changes, with cited evidence.')
    activity = 'extracting'
    prompt_roles = ('system', 'instructions')
    fresh_roles = ('system',)
    reads = ('source', 'triage')
    writes = ('candidate',)
    facts = ('decision_outcome', 'decision_date', 'litigation', 'permission', 'discretion_involved',
             'discretion_central', 'appellants', 'representation', 'municipality', 'units', 'lots',
             'restrictions', 'housing_changes', 'related')

    def applies(self, unit):
        return unit['result'].get('route', {}).get('route') == 'extract'

    def request(self, unit, conversation):
        if conversation is None:
            # A fresh conversation restates the source and the scope decision.
            conversation = Conversation.start(
                self.provider.name, self.prompts['system'],
                unit['case']['source']+'\n\nSCOPE DECISION\n'+json.dumps(unit['result']['triage']))
        conversation = conversation.ask(self.prompts['instructions'])
        return self.provider.render(conversation, self.settings), conversation

    def before_call(self, client, name, request, unit):
        # A saved schema failure may be repairable without a paid retry.
        client.recover_validation(name, request, lambda value: recover_extract(value, unit['pages']))

    def accept(self, unit, value):
        import group_scout as gs
        candidate, warnings = gs.validate_extract(json.dumps(value), unit['pages'], version='v6')
        if candidate is None:
            raise ValueError(str(warnings))
        candidate.pop('scouting', None)
        unit['result']['candidate'] = candidate
        unit['result']['warnings'].extend(warnings)
