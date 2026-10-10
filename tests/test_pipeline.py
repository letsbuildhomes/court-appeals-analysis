"""Step trees, selectors, execution, the ported legacy pipeline, registry and tags; fake models, no network."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import appeal_schema
from pipeline import legacy, registry, selectors
from pipeline.cases import Case
from pipeline.engine import Context, execute
from pipeline.legacy.assemble import Assemble
from pipeline.legacy.review import Review
from pipeline.providers import ChatCompletionsProvider, DecisionProvider
from pipeline.steps import CodeStep, Grouped, Model, ModelStep, Prompt, Sequence, per_case, validate
from pipeline.store import CallStore, Calls

PROMPTS = {'system': 'SYSTEM', 'triage': 'TRIAGE', 'extract': 'EXTRACT', 'primer': 'PRIMER', 'review': 'REVIEW',
           'duration': 'DURATION'}
CITE = [{'doc': 1, 'page': 1, 'quote': 'CASE'}]


def source(case_id):
    return f'<document doc="1" title="t"><page number="1">CASE {case_id} text</page></document>'


def draft():
    return {'schema_version': 'v6', 'dockets': [{
        'docket': 'A', 'related_dockets': [],
        'project': {'description': None, 'municipality': None, 'proposed_units': None, 'proposed_lots': None, 'evidence': []},
        'municipal_decision': {'description': None, 'date': None, 'outcome': 'unclear', 'evidence': []},
        'appellants': [],
        'discretion': {'description': None, 'evidence': [], 'involved': 'unclear', 'central': 'unclear', 'central_evidence': []},
        'timeline': [], 'observed_status': {'litigation': 'unclear', 'permission': 'unclear', 'description': None, 'evidence': []},
        'restrictions': [], 'housing_changes': []}]}


class FakeModel:
    """Answers the legacy pipeline's requests. ``cut`` makes triage of those cases end at the output limit."""

    def __init__(self, cut=()):
        self.cut, self.requests = set(cut), []

    def __call__(self, request):
        self.requests.append(request)
        messages = request['messages']
        finish = 'stop'
        if messages[0]['content'].startswith('PRIMER') and len(messages) == 2:
            cases = json.loads(messages[1]['content'])['cases']
            body = {'reviews': [{'group': c['group'], 'review': self.review(c['group'])} for c in cases]}
        elif messages[0]['content'].startswith('PRIMER'):
            cases = json.loads(messages[-1]['content'])['cases']
            body = {'groups': [{'group': c['group'], 'dockets': [{'docket': 'A', 'note': 'Filed; end unknown', 'end': None,
                    'start': {'date': '2020-01-01', 'relation': 'exact', 'description': 'Filed', 'evidence': CITE}}]}
                    for c in cases]}
        elif len(messages) == 2:
            case_id = messages[1]['content'].split('CASE ')[1].split(' ')[0]
            finish = 'length' if case_id in self.cut else 'stop'
            body = {'schema_version': 'v5-triage', 'documents': [{'doc': 1, 'caption_dockets': [], 'belongs': 'yes'}],
                    'matter': 'm', 'municipal_appeal': 'yes', 'housing': 'no' if case_id == 'excluded' else 'yes',
                    'reason': 'Because', 'evidence': CITE}
        else:
            body = draft()
        response = Mock(status_code=200, text=json.dumps(body))
        response.json.return_value = {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(body)}}],
                                      'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}
        return response

    @staticmethod
    def review(group):
        review = {'schema_version': 'v6-review-1', 'scope': {'verdict': 'agree', 'reason': '', 'evidence': []},
                  'changes': [], 'flags': []}
        if group == 'flag':
            review['scope'] = {'verdict': 'flag', 'reason': 'An enforcement matter, not an appeal', 'evidence': CITE}
        if group == 'bad':
            review['changes'] = [{'docket': 'Z', 'field': 'project', 'severity': 'material', 'reason': 'r',
                                  'evidence': CITE, 'replacement': {}}]
        return review


def passthrough_extract():
    return patch('group_scout.validate_extract', side_effect=lambda raw, pages, version='v6': (json.loads(raw), []))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CallStore(self.root/'calls')

    def run_tree(self, tree, ids, model=None, limit=1.0, run='run', **options):
        model = model or FakeModel()
        cases = [Case(i, source(i), {'source_sha256': 'x'}) for i in ids]
        calls = Calls(self.store, self.root/run/'ledger.jsonl', run=run, limit=limit)
        with patch.object(ChatCompletionsProvider, 'post', side_effect=model), passthrough_extract():
            outputs = execute(tree, cases, Context(calls, workers=4, offline=True, **options))
        return outputs, model, calls

    # Description and editing ------------------------------------------------
    def test_descriptor_carries_prompts_selectors_code_and_children(self):
        descriptor = legacy.build().describe()
        steps = registry.steps(descriptor)
        self.assertEqual(list(steps), ['', 'size-limit', 'first-pass', 'first-pass/triage', 'first-pass/extract',
                                       'finishing', 'finishing/review', 'finishing/duration', 'assemble'])
        review = steps['finishing/review']
        self.assertIn('EDITING', review['description'] + ' EDITING')
        self.assertEqual(review['prompts']['review']['text'], (legacy.PROMPTS/'review.md').read_text())
        self.assertIn('pipeline/legacy/review.py', review['code'])
        self.assertIn('appeal_evidence.py', review['code'])
        self.assertEqual(steps['finishing']['selector']['max_cases'], 2)
        self.assertEqual(steps['first-pass/extract']['conversation'], 'continue')
        self.assertEqual(steps['size-limit']['params'], {'max_chars': 600000})
        self.assertIsNone(steps['assemble']['model'])
        self.assertEqual(registry.identify(legacy.build())[0], registry.identify(legacy.build())[0])

    def test_tree_edits_return_new_trees_and_diff_shows_them(self):
        base = legacy.build()
        before = registry.identify(base)
        changed = base.update('finishing/review', prompts={'review': Prompt('A stricter editor.')})
        changed = changed.remove('finishing/duration').insert('assemble', Assemble('assemble-again'), before=True)
        changed = changed.update('size-limit', params={'max_chars': 400000})
        self.assertEqual(registry.identify(base), before)  # The base tree is untouched.
        rows = {r['path']: r for r in registry.diff(base.describe(), changed.describe())}
        self.assertEqual(rows['finishing/review']['prompts'][0]['value'], 'A stricter editor.')
        self.assertEqual(rows['finishing/duration']['status'], 'removed')
        self.assertEqual(rows['assemble-again']['status'], 'added')
        self.assertEqual(rows['size-limit']['fields'][0]['value'], {'max_chars': 400000})
        self.assertEqual(rows['first-pass/triage']['status'], 'unchanged')
        self.assertIn('finishing/review: review prompt', registry.summary(list(rows.values())))
        with self.assertRaises(KeyError):
            base.find('finishing/absent')

    def test_validation_rejects_bad_structures(self):
        model = Model('deepseek-flash', max_output_tokens=10)
        review = legacy.build().find('finishing/review')
        lone = legacy.build().find('first-pass/extract')
        cases = [
            (Sequence('p', children=[lone]), 'continues only within a group'),
            (Sequence('p', children=[Grouped('g', selector=selectors.sequential(8, 10**6), children=[review])]), 'exceed its limit'),
            (Sequence('p', children=[Assemble('a'), Assemble('a')]), 'unique'),
            (Sequence('p', children=[review.with_(model=Model('unpriced'))]), 'models.json'),
            (Sequence('p', children=[Review('review', model=model)]), 'missing prompts'),
            (Sequence('Bad Name', children=[Assemble('a')]), 'lowercase'),
        ]
        for tree, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                validate(tree)
        validate(legacy.build())

    # Execution ---------------------------------------------------------------
    def test_legacy_pipeline_runs_cases_to_contract_results(self):
        tree = legacy.build(prompt_overrides=PROMPTS)
        outputs, model, calls = self.run_tree(tree, ['excluded', 'flag', 'good-a', 'bad', 'good-b'])
        result = {i: r for i, (r, _, _) in outputs.items()}
        status = {i: s for i, (_, s, _) in outputs.items()}
        self.assertEqual(result['excluded']['disposition']['value'], 'excluded')
        self.assertEqual(result['excluded']['records'], [])
        self.assertEqual(result['flag']['disposition']['value'], 'uncertain')
        self.assertIn('enforcement', result['flag']['disposition']['reason'])
        self.assertEqual((result['flag']['records'], status['flag']['state']), ([], 'complete'))
        self.assertEqual(status['bad']['state'], 'failed')
        self.assertIn('exactly one existing docket', status['bad']['errors'][0]['error'])
        for case_id in ('good-a', 'good-b'):
            self.assertEqual(status[case_id]['state'], 'complete')
            record = result[case_id]['records'][0]
            self.assertEqual((record['docket'], record['duration']['kind']), ('A', 'unknown'))
            self.assertEqual(record['duration']['start']['date'], '2020-01-01')
            self.assertEqual(record['municipal_decision']['outcome'], 'unknown')
            self.assertEqual(appeal_schema.validate(result[case_id]), [])
            self.assertEqual(status[case_id]['quotes']['unverified'], [])
        # Excluded cases are never extracted; finished and failed ones are never measured.
        triage = [r for r in model.requests if len(r['messages']) == 2 and r['messages'][0]['content'] == 'SYSTEM']
        extract = [r for r in model.requests if len(r['messages']) == 4 and r['messages'][0]['content'] == 'SYSTEM']
        reviews = [r for r in model.requests if r['messages'][0]['content'].startswith('PRIMER') and len(r['messages']) == 2]
        durations = [r for r in model.requests if r['messages'][0]['content'].startswith('PRIMER') and len(r['messages']) == 4]
        self.assertEqual((len(triage), len(extract), len(reviews), len(durations)), (5, 4, 2, 2))
        members = sorted(sorted(c['group'] for c in json.loads(r['messages'][1]['content'])['cases']) for r in reviews)
        self.assertEqual(members, [['bad', 'good-b'], ['flag', 'good-a']])
        for duration in durations:
            review = next(r for r in reviews if r['messages'][:2] == duration['messages'][:2])
            self.assertEqual(duration['messages'][:2], review['messages'])  # Continues review's conversation.
            targets = [c['group'] for c in json.loads(duration['messages'][3]['content'])['cases']]
            self.assertEqual(len(targets), 1)
            self.assertTrue(targets[0].startswith('good'))
        trace = outputs['good-a'][2]
        review_call = next(e for e in trace if e['step'] == 'finishing/review' and e['event'] == 'call')
        self.assertEqual(sorted(review_call['members']), ['flag', 'good-a'])
        self.assertEqual(trace[-1], {'step': 'assemble', 'event': 'code'})

    def test_rerunning_reuses_every_call_for_free(self):
        tree = legacy.build(prompt_overrides=PROMPTS)
        first, _, calls = self.run_tree(tree, ['good-a', 'good-b'])
        again, model, rerun = self.run_tree(tree, ['good-a', 'good-b'], limit=0, run='other')
        self.assertEqual(model.requests, [])
        self.assertEqual({i: r for i, (r, _, _) in first.items()}, {i: r for i, (r, _, _) in again.items()})
        self.assertEqual(rerun.spent(), 0)
        self.assertGreater(calls.spent(), 0)
        sampled, model, _ = self.run_tree(tree, ['good-a', 'good-b'], run='noise', samples={'finishing/review': 1})
        # A second sample of review is a new call. This fake answers identically, so the
        # duration request that continues it is unchanged and reused.
        self.assertEqual(len(model.requests), 1)

    def test_batches_wait_until_earlier_steps_settle(self):
        tree = legacy.build(prompt_overrides=PROMPTS)
        outputs, model, _ = self.run_tree(tree, ['good-a', 'cut', 'good-b'], model=FakeModel(cut={'cut'}))
        states = {i: s['state'] for i, (_, s, _) in outputs.items()}
        self.assertEqual(states, {'good-a': 'in_progress', 'cut': 'failed', 'good-b': 'in_progress'})
        self.assertFalse([r for r in model.requests if r['messages'][0]['content'].startswith('PRIMER')])
        self.assertIn('Waiting for 1', outputs['good-a'][2][-1]['reason'])

    def test_budget_refusal_fails_cases_without_sending(self):
        tree = legacy.build(prompt_overrides=PROMPTS)
        outputs, model, _ = self.run_tree(tree, ['good-a'], limit=0)
        status = outputs['good-a'][1]
        self.assertEqual((status['state'], status['errors'][0]['type']), ('failed', 'budget'))
        self.assertEqual(model.requests, [])

    def test_code_steps_can_exclude_cases_before_any_model_call(self):
        class SkipNamed(CodeStep):
            defaults = {'names': ()}

            def apply(self, case, ctx):
                if case.id in self.params['names']:
                    case.exclude(ctx.path, {'reason': 'Listed as out of scope', 'municipal_appeal': 'unknown',
                                            'housing': 'unknown', 'evidence': []})
        tree = legacy.build(prompt_overrides=PROMPTS).insert('size-limit', SkipNamed('skip', names=('good-b',)))
        outputs, model, _ = self.run_tree(tree, ['good-a', 'good-b'])
        self.assertEqual(outputs['good-b'][0]['disposition']['reason'], 'Listed as out of scope')
        self.assertFalse([r for r in model.requests if 'CASE good-b' in json.dumps(r)])
        self.assertEqual(outputs['good-a'][1]['state'], 'complete')

    def test_a_stateless_decision_model_fits_the_step_interface(self):
        class AskHousing(ModelStep):
            description = 'Asks a decision model whether the case concerns housing.'
            fields = ('housing',)

            def request(self, group, conversation, provider):
                case = group.members[0]
                questions = [{'id': 'housing', 'type': 'choice', 'text': 'Housing?', 'options': ['yes', 'no']}]
                return provider.render(case.source, questions), None

            def accept(self, group, value):
                case = group.targets[0]
                answer = value['housing']['choice']

                def update(path):
                    case.set_disposition(path, {'value': 'in_scope' if answer == 'yes' else 'excluded',
                                                'reason': 'Decision model', 'municipal_appeal': 'unknown',
                                                'housing': answer, 'evidence': []})
                return [(case, update)]
        models = {'deepseek-flash': {'provider': 'chat-completions', 'pricing_usd_per_million': {'input': 0.15, 'output': 0.6}},
                  'jev-test': {'provider': 'decision', 'pricing_usd_per_million': {'input': 0.042}}}
        response = Mock(status_code=200, text='{}')
        response.json.return_value = {'answers': {'housing': {'choice': 'yes', 'probabilities': {'yes': .9, 'no': .1}}},
                                      'usage': {'input_tokens': 50}}
        tree = Sequence('decide', children=[AskHousing('ask', model=Model('jev-test', 'JEV'))])
        with patch('pipeline.providers.catalog', return_value=models):
            calls = Calls(self.store, run='decide', limit=1)
            with patch.object(DecisionProvider, 'post', return_value=response) as post:
                outputs = execute(tree, [Case('c', source('c'))], Context(calls, offline=True))
            self.assertEqual(post.call_args.args[0]['state'], source('c'))
            self.assertEqual(outputs['c'][0]['disposition']['housing'], 'yes')
            self.assertAlmostEqual(calls.spent(), 50*0.042/1e6)
            follow = Grouped('g', selector=selectors.each(), children=[
                AskHousing('ask', model=Model('jev-test')), AskHousing('again', model=Model('jev-test'), continues=True)])
            with self.assertRaisesRegex(ValueError, 'cannot continue conversations'):
                validate(Sequence('p', children=[follow]))

    # Registry ---------------------------------------------------------------
    def test_freeze_tags_and_experiment_modules(self):
        pipelines, tags, definitions = self.root/'pipelines', self.root/'tags.jsonl', self.root/'experiments'
        with patch.object(registry, 'PIPELINES', pipelines), patch.object(registry, 'TAGS', tags), \
                patch.object(registry, 'EXPERIMENTS', definitions):
            source = {'module': 'pipeline.legacy', 'options': {'review_batch_cases': 3}}
            pipeline_id = registry.freeze(registry.build(source), source)
            self.assertEqual(registry.freeze(registry.build(source), source), pipeline_id)
            stored = registry.load(pipeline_id)
            self.assertEqual(stored['source'], source)
            self.assertTrue((pipelines/pipeline_id/'sources/pipeline/legacy/review.py').exists())
            registry.set_tag('production', pipeline_id, 'First port')
            folder = definitions/'strict'
            folder.mkdir(parents=True)
            (folder/'strict_review.py').write_text(
                'from pipeline.legacy.review import Review\n\n\nclass StrictReview(Review):\n'
                '    description = "Rejects changes without quotations."\n')
            (folder/'experiment.py').write_text(
                'from .strict_review import StrictReview\n\n\ndef build(base):\n'
                '    review = base.find("finishing/review")\n'
                '    return base.replace("finishing/review", StrictReview("review", model=review.model, prompts=review.prompts))\n')
            experiment = {'experiment': 'strict', 'base': stored['source']}
            tree = registry.build(experiment)
            candidate = registry.freeze(tree, experiment)
            registry.set_tag('production', candidate, 'Adopted strict review', {'experiment': 'strict'})
            self.assertEqual(registry.resolve('production'), candidate)
            self.assertEqual([e['pipeline'] for e in registry.history('production')], [pipeline_id, candidate])
            self.assertEqual(registry.resolve(pipeline_id), pipeline_id)
            row = next(r for r in registry.diff(stored['descriptor'], registry.load(candidate)['descriptor'])
                       if r['path'] == 'finishing/review')
            self.assertEqual(len(row['code']), 1)
            self.assertTrue(row['code'][0].endswith('experiments/strict/strict_review.py'))
            self.assertTrue((pipelines/candidate/'sources'/row['code'][0].lstrip('/')).exists())
            self.assertEqual(row['fields'][0]['field'], 'type')
            with self.assertRaises(KeyError):
                registry.resolve('staging')
            with self.assertRaises(ValueError):
                registry.set_tag('production', candidate, ' ')


if __name__ == '__main__':
    unittest.main()
