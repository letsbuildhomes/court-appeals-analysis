"""Duration bounds: evidenced start and end for each reviewed docket."""
import json

from pipeline.components.base import Component, exact_members, register
from pipeline.conversation import Conversation

CONTINUATION = ('\n\nUse the source documents in the preceding review request. '
                'Assess only the targets below; omit other groups from the earlier batch. '
                'The corrected records below supersede the earlier drafts.')


@register
class Duration(Component):
    name = 'duration'
    summary = ('Finds an evidenced start and end bound for each reviewed docket; '
               'the day count is computed in code, never by the model.')
    scope = 'batch'
    activity = 'measuring'
    prompt_roles = ('instructions',)
    reads = ('source', 'candidate')
    writes = ('duration_bounds',)
    facts = ('duration', 'start', 'end')
    failure_status = 'duration_failed'
    versioned_names = True
    legacy_names = True         # Runs before request hashing named it duration-<batch>.

    def request(self, unit, conversation):
        targets = [{'group': gid, 'candidate': unit['outputs'][gid]['candidate']} for gid in unit['targets']]
        if conversation is None:
            # A fresh conversation carries its own sources.
            sources = {c['group']: c['source'] for c in unit['batch']}
            for target in targets:
                target['source_documents'] = sources[target['group']]
            conversation = Conversation.start(self.provider.name, self.prompts['instructions'],
                                              json.dumps({'cases': targets}, ensure_ascii=False))
        else:
            conversation = conversation.ask(json.dumps(
                {'instructions': self.prompts['instructions']+CONTINUATION, 'cases': targets}, ensure_ascii=False))
        return self.provider.render(conversation, self.settings), conversation

    def check(self, unit, value):
        exact_members(value['groups'], 'group', list(unit['targets']))

    def apply(self, unit, case, result, value):
        from appeal_evidence import interval, source_pages
        records = {r['group']: r for r in value['groups']}[case['group']]['dockets']
        exact_members(records, 'docket', [d['docket'] for d in result['candidate']['dockets']])
        bounds = []
        for record in records:
            validated, warnings = interval(record, source_pages(case['source']))
            bounds.append(validated)
            result['warnings'].extend(warnings)
        result.update(duration_bounds=bounds, status='complete')
        return True
