"""Build deterministic software-test artifacts; no real cases or model calls."""
import argparse
import json
from pathlib import Path

from appeal_evaluation import evaluate
from appeal_reference import validate_reference
from pipeline import registry
from pipeline.steps import CodeStep, Model, ModelStep, Prompt, Sequence, per_case


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures' / 'evaluation'
NOTICE = ('SYNTHETIC SOFTWARE TEST ONLY: invented cases, reference labels, '
          'outputs, progress, and costs. This is not an extraction strategy '
          'experiment or measured research accuracy. No model calls were made.')


class SyntheticExtract(ModelStep):
    description = 'Synthetic step: drafts records from the source. It never runs.'
    fields = ('duration', 'decision_outcome')


class SyntheticCheck(ModelStep):
    description = 'Synthetic step: checks each draft against its source. It never runs.'


class SyntheticFinalize(CodeStep):
    description = 'Synthetic step: assembles final records. It never runs.'


def demo_pipelines():
    """Two small synthetic trees whose diff exercises the dashboard's pipeline view."""
    model = Model('deepseek-flash', 'SYNTHETIC', reasoning='high', max_output_tokens=1000)
    extract = SyntheticExtract('extract', model=model, prompts={'instructions': Prompt('Extract the synthetic facts.')})
    base = Sequence('synthetic', children=[per_case('first-pass', [extract]), SyntheticFinalize('finalize')])
    candidate = base.update('first-pass/extract', prompts={'instructions': Prompt(
        'Extract the synthetic facts, citing a quotation for each.')})
    candidate = candidate.insert('first-pass/extract', SyntheticCheck(
        'check', model=model, prompts={'instructions': Prompt('Check the draft.')}, continues=True))
    (base_id, base_desc), (candidate_id, candidate_desc) = registry.identify(base), registry.identify(candidate)
    return {'baseline': {'id': base_id, 'descriptor': base_desc}, 'candidate': {'id': candidate_id, 'descriptor': candidate_desc},
            'diff': registry.diff(base_desc, candidate_desc)}


def build_demo():
    """Return a fresh deterministic demo envelope suitable for the dashboard."""
    def read(name):
        return json.loads((FIXTURES / (name + '.json')).read_text())

    baseline, candidate, reference = (read(name) for name in
                                      ('baseline', 'candidate', 'reference'))
    validate_reference(reference)
    return {'schema_version': 'appeal-demo-1', 'notice': NOTICE,
            'purpose': 'synthetic_software_test',
            'execution': {'performed': False, 'new_model_calls': 0,
                          'new_model_cost_usd': 0},
            'baseline': baseline, 'candidate': candidate,
            'reference': reference,
            'evaluation': evaluate(baseline, candidate, reference),
            'pipelines': demo_pipelines(), 'sources': read('sources')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path,
                        help='New JSON file; existing files are never replaced')
    args = parser.parse_args()
    artifact = build_demo()
    with args.output.open('x') as handle:
        json.dump(artifact, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
