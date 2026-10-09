"""Build deterministic software-test artifacts; no real cases or model calls."""
import argparse
import json
from pathlib import Path

from appeal_evaluation import evaluate
from appeal_reference import validate_reference


FIXTURES = Path(__file__).parent / 'tests' / 'fixtures' / 'evaluation'
NOTICE = ('SYNTHETIC SOFTWARE TEST ONLY: invented cases, reference labels, '
          'outputs, progress, and costs. This is not an extraction strategy '
          'experiment or measured research accuracy. No model calls were made.')


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
            'sources': read('sources')}


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
