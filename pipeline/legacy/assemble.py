"""Turn reviewed drafts and duration bounds into final records."""
from pipeline.legacy import v6
from pipeline.steps import CodeStep


class Assemble(CodeStep):
    description = ('Converts each remaining case\'s reviewed draft and duration bounds into final records under the '
                   'output contract. A docket without a bound gets an unknown duration.')
    reads = ('draft', 'bounds')
    writes = ('records',)
    code = ('appeal_schema.py', 'pipeline/legacy/v6.py')

    def apply(self, case, ctx):
        records, warnings = v6.records(case.values.get('draft'), case.values.get('bounds'))
        case.records = records
        case.warnings.extend(warnings)
