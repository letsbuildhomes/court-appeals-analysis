"""Content-addressed calls: reuse, spending cap, no automatic resends, repairs, indexes; no network."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from appeal_runtime import read, save
from pipeline.providers import ChatCompletionsProvider, Pricing
from pipeline.store import BudgetRefused, CallStore, Calls, DeliveryFailed, DryRunMiss, build_index, call_key

PRICING = Pricing(input=0.15, output=0.6, cached_input=0.003)


def provider():
    return ChatCompletionsProvider('deepseek-flash', 'deepseek-flash', PRICING, 'http://localhost', 'unused')


def request(text='Return JSON', model='deepseek-flash'):
    return {'model': model, 'stream': False, 'response_format': {'type': 'json_object'}, 'reasoning_effort': 'high',
            'max_tokens': 1000, 'messages': [{'role': 'system', 'content': text}, {'role': 'user', 'content': '{}'}]}


def reply(content, finish='stop'):
    response = Mock(status_code=200, text=content)
    response.json.return_value = {'choices': [{'finish_reason': finish, 'message': {'content': content}}],
                                  'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}
    return response


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CallStore(self.root/'calls')

    def calls(self, limit=1.0, run='run-a', **options):
        return Calls(self.store, self.root/run/'ledger.jsonl', run=run, limit=limit, **options)

    def test_direct_api_configuration(self):
        spec = {'model': 'deepseek-flash', 'env': 'APPEALS'}
        build = lambda **kw: ChatCompletionsProvider.from_spec('default', spec, **kw)
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(build(base_url='https://provider.example/v1/', key='secret').endpoint,
                             'https://provider.example/v1/chat/completions')
            self.assertEqual(build(api_url='https://provider.example/chat/completions', key='secret').endpoint,
                             'https://provider.example/chat/completions')
            with self.assertRaises(ValueError):
                build()
            with self.assertRaises(ValueError):
                build(base_url='https://x', api_url='https://y', key='k')
            with self.assertRaises(ValueError):
                build(api_url='https://user:pass@x/chat', key='k')
            with self.assertRaisesRegex(ValueError, 'No price'):
                ChatCompletionsProvider.from_spec('default', {'model': 'unpriced'}, key='k', base_url='https://x')

    def test_identical_requests_are_reused_across_runs_and_samples_are_distinct(self):
        calls = self.calls()
        with patch.object(ChatCompletionsProvider, 'post', return_value=reply('{"ok": true}')) as post:
            value, key, kind = calls.call(provider(), request())
            self.assertEqual((value, kind), ({'ok': True}, 'new'))
            spent = calls.spent()
            self.assertGreater(spent, 0)
            self.assertEqual(calls.call(provider(), request())[2], 'reused')
            other = self.calls(run='run-b')
            self.assertEqual(other.call(provider(), request())[:3:2], ({'ok': True}, 'reused'))
            self.assertEqual(other.spent(), 0)  # Reused work is free and outside the cap.
            self.assertEqual(post.call_count, 1)
            _, sample_key, kind = calls.call(provider(), request(), sample=1)
            self.assertEqual((kind, post.call_count), ('new', 2))
        self.assertNotEqual(key, sample_key)
        self.assertEqual(key, call_key(provider(), request()))
        self.assertEqual(self.calls().spent(), calls.spent())  # The ledger survives a resume.

    def test_budget_and_uncertain_delivery_do_not_send(self):
        with patch.object(ChatCompletionsProvider, 'post') as post:
            with self.assertRaisesRegex(BudgetRefused, 'cap'):
                self.calls(limit=0).call(provider(), request())
            post.assert_not_called()
        key = call_key(provider(), request())
        req, result, _ = self.store.paths(key)
        save(req, request())
        save(result, {'status': 'pending', 'charged_or_reserved_usd': 0.1, 'run': 'run-a'})
        with patch.object(ChatCompletionsProvider, 'post') as post:
            with self.assertRaisesRegex(DeliveryFailed, 'no automatic resend'):
                self.calls().call(provider(), request())
            post.assert_not_called()

    def test_explicit_retry_archives_the_attempt_and_keeps_its_charge(self):
        calls = self.calls()
        with patch.object(ChatCompletionsProvider, 'post', return_value=reply('{"partial', finish='length')):
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                calls.call(provider(), request())
        first = calls.spent()
        retrying = self.calls(retry_failed=True, retry_max_tokens=4000)
        with patch.object(ChatCompletionsProvider, 'post', return_value=reply('{"ok": true}')) as post:
            value, key, kind = retrying.call(provider(), request())
            self.assertEqual(post.call_args.args[0]['max_tokens'], 4000)
        self.assertEqual((value, kind), ({'ok': True}, 'retried'))
        self.assertEqual(len(list((self.store.root/'attempts').iterdir())), 1)
        self.assertGreater(retrying.spent(), first)  # The failed attempt stays charged.
        with self.assertRaises(DeliveryFailed):
            retrying.call(provider(), request(), retry=True)  # At most once per execution.

    def test_completed_malformed_response_has_one_format_repair(self):
        calls = self.calls()
        with patch.object(ChatCompletionsProvider, 'post', side_effect=[reply('not json'), reply('{"fixed": true}')]) as post:
            self.assertEqual(calls.call(provider(), request())[:3:2], ({'fixed': True}, 'new'))
            self.assertEqual(calls.call(provider(), request())[0], {'fixed': True})
            self.assertEqual(post.call_count, 2)
        state = read(Path(str(self.store.prefix(call_key(provider(), request())))+'.result.json'))
        self.assertTrue(state['json_repair'])

    def test_dry_run_records_new_calls_without_sending_or_writing(self):
        calls = Calls(self.store, run='plan', limit=float('inf'), dry_run=True)
        with patch.object(ChatCompletionsProvider, 'post') as post:
            with self.assertRaises(DryRunMiss):
                calls.call(provider(), request())
            post.assert_not_called()
        self.assertGreater(calls.would_spend(), 0)
        self.assertFalse((self.store.root/'objects').exists())

    def test_index_reads_through_saved_deliveries_without_modifying_them(self):
        legacy = self.root/'legacy'
        for name, state, text in (('ok', {'status': 'ok'}, 'A'), ('rejected', {'status': 'failed', 'error_type': 'validation'}, 'B'),
                                  ('cut', {'status': 'failed', 'error_type': 'output_limit'}, 'C')):
            save(legacy/f'{name}.request.json', request(text))
            save(legacy/f'{name}.result.json', {**state, 'charged_or_reserved_usd': 0.5})
            if name != 'cut':
                save(legacy/f'{name}.parsed.json', {'answer': name})
        before = {p.name: p.read_bytes() for p in legacy.iterdir()}
        summary = build_index(self.store.root, 'legacy', legacy, provider())
        self.assertEqual((summary['indexed'], summary['skipped']), (2, 1))
        store = CallStore(self.store.root)
        calls = Calls(store, run='run-a', limit=0)
        with patch.object(ChatCompletionsProvider, 'post') as post:
            self.assertEqual(calls.call(provider(), request('A'))[:3:2], ({'answer': 'ok'}, 'reused'))
            self.assertEqual(calls.call(provider(), request('B'))[0], {'answer': 'rejected'})
            with self.assertRaises(BudgetRefused):
                calls.call(provider(), request('C'))  # An incomplete delivery is not reused.
            post.assert_not_called()
        self.assertEqual(calls.spent(), 0)
        with patch.object(ChatCompletionsProvider, 'post', return_value=reply('{"answer": "again"}')):
            value, key, kind = Calls(store, run='run-b', limit=1).call(provider(), request('B'), retry=True)
        self.assertEqual((value, kind), ({'answer': 'again'}, 'retried'))
        self.assertEqual(Calls(store, run='run-c').call(provider(), request('B'))[0], {'answer': 'again'})
        self.assertEqual({p.name: p.read_bytes() for p in legacy.iterdir()}, before)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            build_index(self.store.root, 'legacy', legacy, provider())


if __name__ == '__main__':
    unittest.main()
