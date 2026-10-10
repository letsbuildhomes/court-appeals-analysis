"""Scope triage: is this case a municipal appeal about housing, and which documents belong."""
import json

from pipeline.conversation import Conversation
from pipeline.legacy import v6
from pipeline.steps import ModelStep


class Triage(ModelStep):
    description = ('Reads the whole source and decides whether it is a municipal appeal about housing and which '
                   'documents belong. A "no" on either scope question, or no belonging documents, excludes the case; '
                   'otherwise its disposition is in scope or uncertain.')
    prompt_names = ('system', 'instructions')
    reads = ('source',)
    writes = ('triage', 'route', 'disposition')
    fields = ('disposition', 'municipal_appeal', 'housing')
    code = ('group_scout.py', 'json_repair.py', 'pipeline/legacy/v6.py')

    def request(self, group, conversation, provider):
        case = group.members[0]
        conversation = Conversation.start(provider.name, self.prompts['system'].text,
                                          case.source+'\n\n'+self.prompts['instructions'].text)
        return provider.render(conversation, self.model.settings), conversation

    def accept(self, group, value):
        import group_scout as gs
        case = group.targets[0]
        triage, warnings = gs.validate_triage(json.dumps(value), case.pages, version='v5-triage')
        if triage is None:
            raise ValueError(str(warnings))
        route = gs.route_triage(triage, list(case.pages))

        def update(path):
            case.values.update(triage=triage, route=route)
            case.warnings.extend(warnings)
            disposition = v6.scope_disposition(triage, route)
            if disposition['value'] == 'excluded':
                case.exclude(path, disposition)
            else:
                case.set_disposition(path, disposition)
        return [(case, update)]

    def transcript(self, group, value):
        # Later steps see the validated decision, not the raw answer.
        return json.dumps(group.members[0].values['triage'])
