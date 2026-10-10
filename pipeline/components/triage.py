"""Scope triage: is this group a municipal appeal about housing, and what is in it."""
import json

from pipeline.components.base import Component, register
from pipeline.conversation import Conversation


@register
class Triage(Component):
    name = 'triage'
    summary = ('Reads the whole source group and decides whether it is a municipal appeal about housing, '
               'which documents belong, and whether to extract records.')
    activity = 'triaging'
    prompt_roles = ('system', 'instructions')
    reads = ('source',)
    writes = ('triage', 'route')
    facts = ('scope_category', 'municipal_appeal', 'housing')

    def request(self, unit, conversation):
        conversation = Conversation.start(self.provider.name, self.prompts['system'],
                                          unit['case']['source']+'\n\n'+self.prompts['instructions'])
        return self.provider.render(conversation, self.settings), conversation

    def accept(self, unit, value):
        import group_scout as gs
        triage, warnings = gs.validate_triage(json.dumps(value), unit['pages'], version='v5-triage')
        if triage is None:
            raise ValueError(str(warnings))
        unit['result'].update(triage=triage, warnings=warnings, route=gs.route_triage(triage, list(unit['pages'])))
        unit['result']['candidate'] = None

    def transcript(self, unit, value):
        # Later steps see the validated decision, not the raw answer.
        return json.dumps(unit['result']['triage'])
