"""The first production pipeline, ported as steps.

It builds the same requests as the legacy run, so that run's saved calls are
reused for free and an experiment pays only for the steps it changes:

    size-limit   stop sources too long to send whole
    first-pass   per case: triage, then extraction continuing triage's conversation
    finishing    batches of up to 2 drafted cases within 200,000 characters:
                 review, then duration continuing review's conversation
    assemble     final records under the output contract

``build`` takes the legacy run's settings as options, so earlier runs with
other prompts, models or batch sizes can be described too.
"""
from pathlib import Path

from pipeline import selectors
from pipeline.legacy.assemble import Assemble
from pipeline.legacy.duration import Duration
from pipeline.legacy.extract import Extract
from pipeline.legacy.review import Review
from pipeline.legacy.size_limit import SizeLimit
from pipeline.legacy.triage import Triage
from pipeline.steps import Grouped, Model, Prompt, Sequence, per_case

PROMPTS = Path(__file__).with_name('prompts')
NAMES = ('system', 'triage', 'extract', 'primer', 'review', 'duration')


def prompts(overrides=None):
    """The legacy prompts by name; ``overrides`` maps names to replacement text or ``Prompt`` objects."""
    texts = {name: Prompt.load(PROMPTS/f'{name}.md') for name in NAMES}
    for name, value in (overrides or {}).items():
        texts[name] = value if isinstance(value, Prompt) else Prompt(value)
    return texts


def build(prompt_overrides=None, model='deepseek-flash', reasoning='high', max_output_tokens=65536,
          review_max_output_tokens=None, review_batch_cases=2, review_batch_chars=200000, max_chars=600000):
    p = prompts(prompt_overrides)
    first = Model(model, 'APPEALS', reasoning=reasoning, max_output_tokens=max_output_tokens)
    finishing = first.with_settings(max_output_tokens=review_max_output_tokens) if review_max_output_tokens else first
    return Sequence('appeals', description=(
        'Municipal housing appeal extraction: per-case triage and extraction, then batched review and duration.'),
        children=[
            SizeLimit('size-limit', max_chars=max_chars),
            per_case('first-pass', [
                Triage('triage', model=first, prompts={'system': p['system'], 'instructions': p['triage']}),
                Extract('extract', model=first, prompts={'system': p['system'], 'instructions': p['extract']},
                        continues=True),
            ]),
            Grouped('finishing', description=(
                'Batches drafted cases in run order and reviews each batch, then measures duration in the same '
                'conversation. It waits until no case is still stopped at an earlier step, so retried cases '
                'rejoin the same batches.'),
                selector=selectors.sequential(review_batch_cases, review_batch_chars, measure='draft',
                                              requires=('draft',)),
                wait=True, children=[
                    Review('review', model=finishing, prompts={k: p[k] for k in ('primer', 'extract', 'triage', 'review')}),
                    Duration('duration', model=finishing, prompts={'instructions': p['duration']}, continues=True),
                ]),
            Assemble('assemble'),
        ])
