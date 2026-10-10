"""Editorial review of drafted records, one call per batch of cases."""
import json

from pipeline.components.base import Component, exact_members, register
from pipeline.conversation import Conversation

BATCH_EDITING = '''Review this batch as one research editor. Apply the same definitions consistently,
but never transfer facts, dates, people or citations between groups. Doc/page numbering
is local to each group. Return JSON {"reviews":[{"group":"exact group id","review":{...}}]}.
Include every supplied group exactly once, with a complete v6-review-1 patch.'''


@register
class Review(Component):
    name = 'review'
    summary = ('A research editor checks each draft against its source, correcting fields and citations, '
               'or flags the case when its scope is doubtful.')
    scope = 'batch'
    activity = 'reviewing'
    prompt_roles = ('primer', 'extract', 'triage', 'review')
    reads = ('source', 'triage', 'candidate')
    writes = ('review', 'candidate')
    facts = ('scope_category', 'decision_outcome', 'decision_date', 'litigation', 'permission',
             'discretion_involved', 'discretion_central', 'appellants', 'representation', 'municipality',
             'units', 'lots', 'restrictions', 'housing_changes', 'related')
    failure_status = 'review_failed'

    def request(self, unit, conversation):
        p = self.prompts
        system = (p['primer']+'\n\nEXTRACTION CONTRACT\n'+p['extract']+'\n\nSCOPE CONTRACT\n'+p['triage']
                  + '\n\nEDITING CONTRACT\n'+p['review']+'\n\nBATCH EDITING\n'+BATCH_EDITING)
        # Every batch member is in context, including finished ones, so a retry
        # repeats the identical request.
        data = {'cases': [{'group': c['group'], 'source_documents': c['source'], 'triage': c['triage'],
                           'candidate': c['candidate']} for c in unit['batch']]}
        conversation = Conversation.start(self.provider.name, system, json.dumps(data, ensure_ascii=False))
        return self.provider.render(conversation, self.settings), conversation

    def check(self, unit, value):
        exact_members(value['reviews'], 'group', [c['group'] for c in unit['batch']])

    def apply(self, unit, case, result, value):
        from appeal_evidence import apply_review, source_pages
        review = {r['group']: r['review'] for r in value['reviews']}[case['group']]
        candidate, warnings = apply_review(case['candidate'], review, source_pages(case['source']))
        result['review'] = review
        result['warnings'].extend(warnings)
        if review['scope']['verdict'] == 'flag':
            result['status'] = 'scope_flagged'
            return False
        result.update(candidate=candidate, status='reviewed')
        return True
