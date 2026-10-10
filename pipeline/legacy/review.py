"""Editorial review of drafted records, one call per batch of cases."""
import json

from pipeline.conversation import Conversation
from pipeline.legacy import v6
from pipeline.steps import ModelStep

BATCH_EDITING = '''Review this batch as one research editor. Apply the same definitions consistently,
but never transfer facts, dates, people or citations between groups. Doc/page numbering
is local to each group. Return JSON {"reviews":[{"group":"exact group id","review":{...}}]}.
Include every supplied group exactly once, with a complete v6-review-1 patch.'''


class Review(ModelStep):
    description = ('A research editor checks each draft against its source and corrects fields and citations, '
                   'or flags a doubtful scope decision, which leaves the case uncertain for human review. Its system '
                   'prompt joins the primer with the extraction, scope and editing contracts.')
    prompt_names = ('primer', 'extract', 'triage', 'review')
    reads = ('source', 'triage', 'draft')
    writes = ('review', 'draft', 'disposition')
    fields = ('disposition', 'decision_outcome', 'decision_date', 'litigation', 'permission', 'discretion_involved',
              'discretion_central', 'appellants', 'representation', 'municipality', 'units', 'lots', 'restrictions',
              'housing_changes', 'related')
    max_group = 6
    code = ('appeal_evidence.py', 'group_scout.py', 'json_repair.py', 'pipeline/legacy/v6.py')

    def request(self, group, conversation, provider):
        p = {k: v.text for k, v in self.prompts.items()}
        system = (p['primer']+'\n\nEXTRACTION CONTRACT\n'+p['extract']+'\n\nSCOPE CONTRACT\n'+p['triage']
                  + '\n\nEDITING CONTRACT\n'+p['review']+'\n\nBATCH EDITING\n'+BATCH_EDITING)
        # Every member is in context, including ones a retry does not update,
        # so a retry repeats the identical request.
        data = {'cases': [{'group': c.id, 'source_documents': c.source, 'triage': c.values['triage'],
                           'candidate': c.values['draft']} for c in group.members]}
        conversation = Conversation.start(provider.name, system, json.dumps(data, ensure_ascii=False))
        return provider.render(conversation, self.model.settings), conversation

    def accept(self, group, value):
        from appeal_evidence import apply_review
        v6.exact_members(value['reviews'], 'group', [c.id for c in group.members])
        reviews = {r['group']: r['review'] for r in value['reviews']}
        outcomes = []
        for case in group.targets:
            try:
                review = reviews[case.id]
                candidate, warnings = apply_review(case.values['draft'], review, case.pages)
            except Exception as exc:
                outcomes.append((case, exc))
                continue
            outcomes.append((case, self.updater(case, review, candidate, warnings)))
        return outcomes

    @staticmethod
    def updater(case, review, candidate, warnings):
        def update(path):
            case.values['review'] = review
            case.warnings.extend(warnings)
            if review['scope']['verdict'] == 'flag':
                case.set_disposition(path, v6.flagged_disposition(case.disposition, review))
                case.finish(path)
            else:
                case.values['draft'] = candidate
        return update
