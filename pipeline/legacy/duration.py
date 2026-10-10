"""Duration bounds: an evidenced start and end for each reviewed docket."""
import json

from pipeline.conversation import Conversation
from pipeline.legacy import v6
from pipeline.steps import ModelStep

CONTINUATION = ('\n\nUse the source documents in the preceding review request. '
                'Assess only the targets below; omit other groups from the earlier batch. '
                'The corrected records below supersede the earlier drafts.')


class Duration(ModelStep):
    description = ('Finds an evidenced start and end bound for each reviewed docket. Continuing review\'s '
                   'conversation, it refers back to the sources already in the review request. Code subtracts '
                   'the dates; the model never counts days.')
    prompt_names = ('instructions',)
    reads = ('source', 'draft')
    writes = ('bounds',)
    fields = ('duration', 'start', 'end')
    max_group = 6
    code = ('appeal_evidence.py', 'group_scout.py', 'json_repair.py', 'pipeline/legacy/v6.py')

    def request(self, group, conversation, provider):
        targets = [{'group': c.id, 'candidate': c.values['draft']} for c in group.targets]
        instructions = self.prompts['instructions'].text
        if conversation is None:
            # A fresh conversation carries its own sources.
            for target, case in zip(targets, group.targets):
                target['source_documents'] = case.source
            conversation = Conversation.start(provider.name, instructions,
                                              json.dumps({'cases': targets}, ensure_ascii=False))
        else:
            conversation = conversation.ask(json.dumps(
                {'instructions': instructions+CONTINUATION, 'cases': targets}, ensure_ascii=False))
        return provider.render(conversation, self.model.settings), conversation

    def accept(self, group, value):
        from appeal_evidence import interval
        v6.exact_members(value['groups'], 'group', [c.id for c in group.targets])
        answers = {r['group']: r for r in value['groups']}
        outcomes = []
        for case in group.targets:
            try:
                records = answers[case.id]['dockets']
                v6.exact_members(records, 'docket', [d['docket'] for d in case.values['draft']['dockets']])
                bounds, warnings = [], []
                for record in records:
                    validated, notes = interval(record, case.pages)
                    bounds.append(validated)
                    warnings.extend(notes)
            except Exception as exc:
                outcomes.append((case, exc))
                continue
            outcomes.append((case, self.updater(case, bounds, warnings)))
        return outcomes

    @staticmethod
    def updater(case, bounds, warnings):
        def update(path):
            case.values['bounds'] = bounds
            case.warnings.extend(warnings)
        return update
