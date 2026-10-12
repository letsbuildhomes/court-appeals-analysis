"""Duration bounds, as the September run found them: an evidenced start and end for each
reviewed docket, continuing each review batch's conversation. Code subtracts the dates."""
import json

from court.contracts import cases as case_contract
from court.core.models import Conversation
from court.core.step import Failure, Step, is_failure

CONTINUATION = ('\n\nUse the source documents in the preceding review request. '
                'Assess only the targets below; omit other groups from the earlier batch. '
                'The corrected records below supersede the earlier drafts.')


class Duration(Step):
    description = ('Finds an evidenced start and end bound for each reviewed docket. Continuing review\'s '
                   'conversation, it refers back to the sources already in the review request. Code subtracts '
                   'the dates; the model never counts days.')
    inputs = {'cases': 'cases', 'reviews': None}
    prompts = ('instructions',)
    defaults = {'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536, 'continue': True}
    uses = ('legacy/v6:conversion', 'legacy/v6:review-patch')

    def run(self, inputs, ctx):
        cases, reviews = inputs['cases'], inputs['reviews']
        targets = {}
        for key in sorted(reviews):
            review = reviews[key]
            if not is_failure(review) and not review['flagged']:
                targets.setdefault(tuple(review['batch']), []).append(key)
        output = {}
        for result in ctx.map(lambda item: self.batch(item[1], cases, reviews, ctx), targets.items()):
            output.update(result)
        return output

    def batch(self, keys, cases, reviews, ctx):
        v6, patch = self.use('legacy/v6:conversion'), self.use('legacy/v6:review-patch')
        provider = ctx.provider(self.settings)
        targets = [{'group': k, 'candidate': reviews[k]['draft']} for k in keys]
        instructions = self.prompt('instructions')
        if self.settings['continue']:
            conversation = Conversation(provider.name, tuple(tuple(m) for m in reviews[keys[0]]['conversation']))
            conversation = conversation.ask(json.dumps({'instructions': instructions+CONTINUATION, 'cases': targets},
                                                       ensure_ascii=False))
        else:
            # A fresh conversation carries its own sources.
            for target, key in zip(targets, keys):
                target['source_documents'] = cases[key]['text']
            conversation = Conversation.start(provider.name, instructions,
                                              json.dumps({'cases': targets}, ensure_ascii=False))

        def accept(value):
            v6.exact_members(value['groups'], 'group', keys)
            return value
        value = ctx.ask(keys, self.settings, provider.render(conversation, self.settings), accept)
        if is_failure(value):
            return {k: value for k in keys}
        answers = {r['group']: r for r in value['groups']}
        output = {}
        for key in keys:
            try:
                records = answers[key]['dockets']
                v6.exact_members(records, 'docket', [d['docket'] for d in reviews[key]['draft']['dockets']])
                bounds = []
                pages = case_contract.pages(cases[key]['text'])
                for record in records:
                    validated, notes = patch.interval(record, pages)
                    bounds.append(validated)
                    for note in notes:
                        ctx.note(key, 'warning', text=note)
            except Exception as exc:
                output[key] = Failure('validation', exc, ctx.path)
                ctx.note(key, 'rejected', reason=str(exc))
                continue
            output[key] = {'bounds': bounds}
        return output
