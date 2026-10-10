"""Recipes, conversations, batchers and providers; no network."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import group_scout as gs
from appeal_runtime import Client, read
from pipeline import batching, recipe as recipes
from pipeline.conversation import Conversation
from pipeline.providers import ChatCompletionsProvider, DecisionProvider, Pricing, build

PROMPTS = {k: k.upper() for k in ('system', 'triage', 'extract', 'primer', 'review', 'duration')}


class RecipeTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = recipes.legacy_recipe({'model': 'deepseek-flash'}, PROMPTS)

    def test_base_recipe_matches_the_legacy_description_of_existing_runs(self):
        recipe, prompts = recipes.load(recipes.BASE)
        recipes.validate(recipe)
        self.assertEqual(set(prompts), set(PROMPTS))
        legacy = recipes.legacy_recipe({'model': 'deepseek-flash', 'reasoning': 'high', 'max_output_tokens': 65536,
                                        'review_batch_chars': 200000}, prompts)
        self.assertEqual({k: v for k, v in legacy.items() if k != 'legacy'}, recipe)
        tokens = recipes.legacy_recipe({'model': 'deepseek-flash', 'review_max_output_tokens': 131072}, PROMPTS)
        self.assertEqual(recipes.step_spec(tokens, 'duration')['settings']['max_output_tokens'], 131072)
        self.assertEqual(recipes.step_spec(tokens, 'triage')['settings']['max_output_tokens'], 65536)

    def test_compose_merges_fragments_swaps_prompts_and_loads_components(self):
        overlay = self.root/'experiment'
        (overlay/'prompts').mkdir(parents=True)
        (overlay/'prompts'/'review.md').write_text('NEW REVIEW')
        (overlay/'checklist.md').write_text('CHECKLIST')
        (overlay/'recipe.json').write_text(json.dumps({
            'prompts': {'checklist': 'checklist.md'},
            'steps': {'extract': {'context': 'fresh'}, 'duration': {'settings': {'reasoning': None}}},
            'phases': {'finishing': {'batch': {'kind': 'single', 'max_cases': None}}}}))
        recipe, prompts, components, sources = recipes.compose(self.base, PROMPTS, overlay)
        recipes.validate(recipe)
        self.assertEqual(prompts['review'], 'NEW REVIEW')
        self.assertEqual(prompts['checklist'], 'CHECKLIST')
        self.assertEqual(recipe['steps']['extract']['context'], 'fresh')
        self.assertEqual(recipe['phases']['finishing']['batch'], {'kind': 'single', 'max_chars': 200000})
        self.assertEqual(sorted(sources['prompts']), ['checklist', 'review'])
        self.assertEqual(self.base['steps']['extract']['context'], 'continue')  # The base is not modified.
        changes = recipes.diff(self.base, recipe)
        self.assertIn({'path': 'steps.extract.context', 'base': 'continue', 'value': 'fresh'}, changes)
        described = recipes.describe(recipe)
        steps = {s['id']: s for p in described['phases'] for s in p['steps']}
        self.assertEqual(steps['extract']['context'], 'fresh')
        self.assertEqual(steps['duration']['facts'], ['duration', 'start', 'end'])
        self.assertEqual(steps['review']['prompts']['review']['sha256'], recipes.digest('NEW REVIEW'))
        self.assertEqual(described['phases'][1]['batch']['text'], 'One case per call')

    def test_validation_reports_structural_problems(self):
        def invalid(change, message):
            with self.assertRaisesRegex(ValueError, message):
                recipes.validate(recipes.deep_merge(self.base, change))
        invalid({'steps': {'triage': {'context': 'continue'}}}, 'nothing to continue')
        invalid({'steps': {'review': {'component': 'missing'}}}, 'unknown component')
        invalid({'steps': {'review': {'component': 'triage'}}}, 'case component cannot run in a batched')
        invalid({'steps': {'duration': {'prompts': {'instructions': 'absent'}}}}, "role 'instructions' is missing")
        invalid({'providers': {'default': {'model': 'nobody'}}}, 'no entry in pipeline/models.json')
        invalid({'phases': {'finishing': {'batch': {'kind': 'random'}}}}, 'unknown batcher')
        invalid({'order': ['finishing', 'first_pass']}, 'Order must be one per_case phase')
        invalid({'steps': {'extract': {'context': 'sometimes'}}}, 'context must be fresh or continue')

    def test_conversations_are_immutable_and_keep_their_prefix(self):
        start = Conversation.start('default', 'SYSTEM', 'SOURCE')
        later = start.answer('{"a": 1}').ask('NEXT')
        self.assertEqual(len(start.messages), 2)
        self.assertEqual(later.payload()[:2], start.payload())
        self.assertNotEqual(start.sha256, later.sha256)

    def test_batchers(self):
        cases = [{'source': 'x'*10, 'candidate': None} for _ in range(5)]
        self.assertEqual([len(b) for b in batching.batches(cases, {'kind': 'sequential', 'max_cases': 2})], [2, 2, 1])
        self.assertEqual([len(b) for b in batching.batches(cases, {'kind': 'single'})], [1]*5)
        self.assertEqual([len(b) for b in batching.batches(cases, {'kind': 'sequential', 'max_cases': 9, 'max_chars': 30})], [2, 2, 1])
        with self.assertRaises(ValueError):
            batching.batches(cases, {'kind': 'random'})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_chat_costs_match_the_previous_accounting(self):
        provider = build('default', {'model': 'deepseek-flash'}, offline=True)
        request = gs.triage_payload('deepseek-flash', PROMPTS, 'Source text é', 'high')
        request['max_tokens'] = 65536
        self.assertAlmostEqual(provider.bound(request), gs.request_cost_bound(request, 1))
        usage = {'prompt_tokens': 1000, 'completion_tokens': 200, 'prompt_cache_hit_tokens': 400}
        self.assertAlmostEqual(provider.price(usage), gs.cost_usd(usage))
        self.assertIsNone(provider.price({'total_tokens': 5}))
        conversation = Conversation.start('default', 'SYSTEM', 'USER')
        self.assertEqual(provider.render(conversation, {'reasoning': 'high', 'max_output_tokens': 10})['messages'],
                         conversation.payload())

    def test_a_decision_model_fits_the_same_capped_client(self):
        """A stateless typed-question model: input-only pricing, no conversation, no JSON repair."""
        provider = DecisionProvider('judge', 'decision-test', Pricing(input=0.042), 'https://decisions.example/v1', 'key')
        request = provider.render({'record': 'Appeal of a permit'}, {'scope': {'type': 'choice', 'criteria': {'yes': None, 'no': None}}})
        self.assertNotIn('messages', request)
        self.assertGreater(provider.bound(request), 0)
        self.assertNotIn('conversation', provider.capabilities)
        response = Mock(status_code=200, text='{}')
        response.json.return_value = {'model': 'decision-test', 'answers': {'scope': {'choice': 'yes', 'confidence': 0.9}},
                                      'usage': {'input_tokens': 1000, 'output_tokens': 4}}
        with patch('appeal_runtime.httpx.Client') as http:
            http.return_value.__enter__.return_value.post.return_value = response
            capped = Client(self.root, provider, 0)
            with self.assertRaisesRegex(ValueError, 'cap'):
                capped.call('case.scope', request)
            http.assert_not_called()
            client = Client(self.root, provider, 1)
            self.assertEqual(client.call('case.scope', request), {'scope': {'choice': 'yes', 'confidence': 0.9}})
        state = read(self.root/'case.scope.result.json')
        self.assertAlmostEqual(state['charged_or_reserved_usd'], 1000*0.042/1_000_000)
        self.assertEqual(state['provider'], {'provider': 'judge', 'kind': 'decision', 'model': 'decision-test'})

    def test_recipes_refuse_steps_a_provider_cannot_serve(self):
        catalog = self.root/'models.json'
        catalog.write_text(json.dumps({'models': {
            'deepseek-flash': {'provider': 'chat-completions', 'pricing_usd_per_million': {'input': 0.15, 'output': 0.6}},
            'decision-test': {'provider': 'decision', 'pricing_usd_per_million': {'input': 0.042}}}}))
        base = recipes.legacy_recipe({'model': 'deepseek-flash'}, PROMPTS)
        with patch('pipeline.providers.MODELS_PATH', catalog):
            decision = recipes.deep_merge(base, {'providers': {'default': {'model': 'decision-test'}}})
            with self.assertRaisesRegex(ValueError, 'needs a chat provider'):
                recipes.validate(decision)
            self.assertEqual(build('judge', {'model': 'decision-test'}, offline=True).kind, 'decision')

    def test_inherited_calls_are_free_and_only_identical_requests_match(self):
        provider = ChatCompletionsProvider('default', 'model', Pricing(input=0.15, output=0.6), 'http://localhost', 'k')
        base = self.root/'base'
        base.mkdir()
        request = {'model': 'model', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': 'x'}]}
        (base/'old.request.json').write_text(json.dumps(request))
        (base/'old.result.json').write_text(json.dumps({'status': 'ok', 'charged_or_reserved_usd': 0.5}))
        (base/'old.parsed.json').write_text(json.dumps({'answer': 1}))
        client = Client(self.root/'run', provider, 0, inherit=base)
        with patch('appeal_runtime.httpx.Client') as http:
            self.assertEqual(client.call('new', request, aliases=('old',)), {'answer': 1})
            http.assert_not_called()
        self.assertEqual(read(self.root/'run/new.result.json')['inherited_from'], str(base/'old'))
        self.assertEqual(client.spent(), 0)
        with self.assertRaisesRegex(ValueError, 'cap'):
            client.call('other', {**request, 'max_tokens': 11}, aliases=('old',))


if __name__ == '__main__':
    unittest.main()
