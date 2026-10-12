"""Turn the September pipeline's decisions into results under the results contract.

An excluded case has its disposition and no records. A case review flagged
is uncertain, with no records. Every other case gets one record per reviewed
docket, with the duration bounds found for it (unknown when none were).
Unverified quotes are noted in the trace.
"""
from court.contracts import cases as case_contract, results
from court.core.step import Step, is_failure


class Assemble(Step):
    description = ('Converts each case\'s reviewed draft and duration bounds into final records under the results '
                   'contract. A docket without a bound gets an unknown duration.')
    inputs = {'cases': 'cases', 'triage': None, 'reviews': None, 'durations': None}
    output = 'results'
    uses = ('legacy/v6:conversion',)

    def run(self, inputs, ctx):
        v6 = self.use('legacy/v6:conversion')
        output = {}
        for key, triage in inputs['triage'].items():
            if is_failure(triage):
                output[key] = triage
                continue
            if triage['excluded']:
                output[key] = results.result(key, triage['disposition'])
                continue
            review = inputs['reviews'].get(key)
            if review is None or is_failure(review):
                continue
            if review['flagged']:
                output[key] = results.result(key, review['disposition'])
                continue
            duration = inputs['durations'].get(key)
            if duration is None or is_failure(duration):
                continue
            records, warnings = v6.records(review['draft'], duration['bounds'])
            for warning in warnings:
                ctx.note(key, 'warning', text=warning)
            output[key] = results.result(key, review['disposition'], records)
            checked, unverified = results.verify_quotes(output[key], case_contract.pages(inputs['cases'][key]['text']))
            ctx.note(key, 'quotes', checked=checked, unverified=unverified or None)
        return output
