"""Fail cases whose text is too long to send whole, rather than truncating them."""
from court.core.step import Failure, Step


class RefuseLong(Step):
    description = ('Stops cases whose text exceeds a character limit, rather than truncating it; '
                   'such a case needs a separate route.')
    inputs = {'cases': 'cases'}
    defaults = {'max_chars': 600000}

    def run(self, inputs, ctx):
        limit = self.settings['max_chars']
        return {key: Failure('too_large', f'Source exceeds {limit:,} characters; a separate route is required '
                                          '(source not truncated)', ctx.path) if len(case['text']) > limit else case
                for key, case in inputs['cases'].items()}
