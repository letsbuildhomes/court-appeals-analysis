"""Stop cases whose source is too long to send whole."""
from pipeline.steps import CodeStep


class SizeLimit(CodeStep):
    description = ('Stops cases whose source text exceeds a character limit, rather than truncating it; '
                   'such a case needs a separate route.')
    reads = ('source',)
    defaults = {'max_chars': 600000}

    def apply(self, case, ctx):
        if len(case.source) > self.params['max_chars']:
            case.fail(ctx.path, f'Source exceeds {self.params["max_chars"]:,} characters; '
                                'a separate route is required (source not truncated)', 'too_large', retryable=False)
