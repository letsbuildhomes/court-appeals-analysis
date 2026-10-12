"""The September extraction: per-case triage and drafting, then batched review and duration.

    size-limit   fail cases too long to send whole
    triage       is it a municipal housing appeal; excluded cases stop here
    draft        one draft record per docket, continuing triage's conversation
    review       batches of drafted cases: correct drafts or flag scope decisions
    duration     evidenced start and end bounds, continuing each review batch's conversation
    assemble     results under the results contract

A case that fails at any step is a failure in the results, with the step and
reason. Every case reaches review only after all drafting is done, so batches
are formed from the complete set of drafted cases.
"""
from court.core.step import Step, is_failure


def only(values, keys):
    return {k: values[k] for k in keys}


def ok(values):
    return [k for k, v in values.items() if not is_failure(v)]


class LegacyExtract(Step):
    description = 'Per-case triage and drafting, then batched review and duration, then assembly into results.'
    inputs = {'cases': 'cases'}
    output = 'results'
    children = ('size-limit', 'triage', 'draft', 'review', 'duration', 'assemble')

    def run(self, inputs, ctx):
        cases = inputs['cases']
        failures = {k: v for k, v in cases.items() if is_failure(v)}
        sized = ctx.run(self.child('size-limit'), {'cases': cases})
        failures.update({k: v for k, v in sized.items() if is_failure(v)})
        triage = ctx.run(self.child('triage'), {'cases': only(cases, ok(sized))})
        continuing = [k for k in ok(triage) if not triage[k]['excluded']]
        drafts = ctx.run(self.child('draft'), {'cases': only(cases, continuing), 'triage': only(triage, continuing)})
        drafted = ok(drafts)
        reviews = ctx.run(self.child('review'), {'cases': only(cases, drafted), 'triage': only(triage, drafted),
                                                 'drafts': only(drafts, drafted)})
        reviewed = [k for k in ok(reviews) if not reviews[k]['flagged']]
        durations = ctx.run(self.child('duration'), {'cases': only(cases, reviewed),
                                                     'reviews': only(reviews, reviewed)})
        for stage in (drafts, reviews, durations):
            failures.update({k: v for k, v in stage.items() if is_failure(v)})
        assembled = ctx.run(self.child('assemble'), {'cases': cases, 'triage': triage, 'reviews': reviews,
                                                     'durations': durations})
        output = {**assembled, **failures}
        missing = set(cases) - set(output)
        if missing:
            raise RuntimeError(f'{len(missing)} cases have neither a result nor a failure')
        return dict(sorted(output.items()))
