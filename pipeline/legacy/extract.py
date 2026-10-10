"""Record extraction: one draft v6 record per municipal appeal docket."""
import copy
import json

from pipeline.conversation import Conversation
from pipeline.steps import ModelStep


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


class Extract(ModelStep):
    description = ('Drafts one record per municipal appeal docket: project, decision, appellants, discretion, '
                   'timeline, observed status, restrictions and housing changes, each with cited evidence. '
                   'Continuing triage\'s conversation, it sees the validated scope decision.')
    prompt_names = ('system', 'instructions')
    reads = ('source', 'triage')
    writes = ('draft',)
    fields = ('decision_outcome', 'decision_date', 'litigation', 'permission', 'discretion_involved',
              'discretion_central', 'appellants', 'representation', 'municipality', 'units', 'lots',
              'restrictions', 'housing_changes', 'related')
    code = ('group_scout.py', 'json_repair.py')

    def request(self, group, conversation, provider):
        case = group.members[0]
        if conversation is None:
            # A fresh conversation restates the source and the scope decision.
            conversation = Conversation.start(
                provider.name, self.prompts['system'].text,
                case.source+'\n\nSCOPE DECISION\n'+json.dumps(case.values['triage']))
        conversation = conversation.ask(self.prompts['instructions'].text)
        return provider.render(conversation, self.model.settings), conversation

    def accept(self, group, value):
        import group_scout as gs
        case = group.targets[0]
        candidate, warnings = gs.validate_extract(json.dumps(value), case.pages, version='v6')
        if candidate is None:
            # A misplaced field may be recoverable without another call.
            normalized, _ = normalize_extract(value)
            candidate, retried = gs.validate_extract(json.dumps(normalized), case.pages, version='v6')
            if candidate is None:
                raise ValueError(str(warnings))
            warnings = retried
        candidate.pop('scouting', None)

        def update(path):
            case.values['draft'] = candidate
            case.warnings.extend(warnings)
        return [(case, update)]
