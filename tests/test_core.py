"""The framework: datasets, the library, pipeline resolution and tags, the runner and the models service."""
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from court.config import ROOT
from court.core.context import Cache
from court.core.datasets import Datasets, Failure, diff
from court.core.library import Library, LibraryError
from court.core.models import CallStore, Models, BudgetRefused, DeliveryFailed, NotSent, Rejected, provider_for
from court.core.pipelines import PipelineError, Pipelines, diff as pipeline_diff
from court.core.runner import Runner, RunError

UPPER = '''
from court.core.step import Step, Failure


class Upper(Step):
    inputs = {'input': None}
    defaults = {'suffix': ''}

    def run(self, inputs, ctx):
        out = {}
        for key, value in inputs['input'].items():
            if value == 'bad':
                out[key] = Failure('validation', 'bad item', ctx.path)
            else:
                out[key] = value.upper()+self.settings['suffix']
                ctx.note(key, 'upper')
        return out
'''
COUNT = '''
from court.core.step import Step, is_failure


class Count(Step):
    inputs = {'input': None}
    prompts = ('template',)
    uses = ('shared/text:helpers',)

    def run(self, inputs, ctx):
        helpers = self.use('shared/text:helpers')
        return {k: v if is_failure(v) else self.prompt('template').strip().format(helpers.length(v))
                for k, v in inputs['input'].items()}
'''
HELPERS = '''
def length(text):
    return len(text)
'''


class Fixture:
    def __init__(self, tmp):
        self.root = Path(tmp)
        for name, text in {'toy/upper.py': UPPER, 'toy/count.py': COUNT, 'shared/text/helpers.py': HELPERS}.items():
            path = self.root/'steps'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.root/'steps/flow').mkdir(parents=True)
        shutil.copy(ROOT/'steps/flow/sequence.py', self.root/'steps/flow/sequence.py')
        (self.root/'prompts/count').mkdir(parents=True)
        (self.root/'prompts/count/plain.md').write_text('{} characters\n')
        (self.root/'models.json').write_text(json.dumps({'models': {'fake': {
            'provider': 'chat-completions', 'pricing_usd_per_million': {'input': 1.0, 'output': 1.0}}}}))
        self.library = Library(self.root)
        self.pipelines = Pipelines(self.root/'pipelines', self.library)
        self.datasets = Datasets(self.root/'datasets')
        self.store = CallStore(self.root/'calls')
        self.runner = Runner(self.pipelines, self.datasets, self.root/'runs', self.store, Cache(self.root/'cache'))

    def tree(self, suffix=''):
        return {'name': 'toy', 'step': 'flow:sequence', 'children': [
            {'name': 'upper', 'step': 'toy:upper', 'settings': {'suffix': suffix}, 'save': 'shouted'},
            {'name': 'count', 'step': 'toy:count', 'prompts': {'template': 'count:plain'}}]}


class DatasetTests(unittest.TestCase):
    def test_identity_comes_from_contents(self):
        with TemporaryDirectory() as tmp:
            store = Datasets(tmp)
            a = store.write('words', {'b': 'two', 'a': 'one'}, producer={'run': 'x'})
            b = store.write('words', {'a': 'one', 'b': 'two'}, producer={'run': 'y'})
            self.assertEqual(a.id, b.id)
            self.assertEqual(a.manifest['producer'], {'run': 'x'})
            c = store.write('words', {'a': 'one', 'b': 'three', 'c/1': Failure('validation', 'no')})
            self.assertNotEqual(a.id, c.id)
            self.assertEqual(diff(a, c), {'identical': ['a'], 'changed': ['b'], 'removed': [], 'added': ['c/1']})
            self.assertEqual(c.get('c/1'), Failure('validation', 'no'))
            self.assertEqual(c.verify(), [])
            self.assertNotEqual(store.write('other', {'a': 'one', 'b': 'two'}).id, a.id)

    def test_names_and_bad_keys(self):
        with TemporaryDirectory() as tmp:
            store = Datasets(tmp)
            a = store.write('words', {'a': 1})
            store.name('first', a.id)
            self.assertEqual(store.open('first').id, a.id)
            with self.assertRaises(ValueError):
                store.write('words', {'../x': 1})

    def test_drafts_resume(self):
        with TemporaryDirectory() as tmp:
            store = Datasets(tmp)
            draft = store.draft('words', name='resumable')
            draft.put('a', 1)
            draft.checkpoint()
            again = store.draft('words', name='resumable')
            self.assertIn('a', again)
            again.put('b', 2)
            self.assertEqual(again.finish().load(), {'a': 1, 'b': 2})


class LibraryTests(unittest.TestCase):
    def test_names_map_to_files_and_one_step_each(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            self.assertEqual(f.library.entry_path('shared/text:helpers'), f.root/'steps/shared/text/helpers.py')
            self.assertIn('toy:upper', f.library.entries())
            self.assertEqual(f.library.uses('toy:count'), ['shared/text:helpers'])
            with self.assertRaises(LibraryError):
                f.library.step_class('shared/text:helpers')
            with self.assertRaises(LibraryError):
                f.library.entry_path('toy/upper')

    def test_undeclared_helpers_are_refused(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            (f.root/'steps/shared/text/sneaky.py').write_text("USES = ()\nhelpers = use('shared/text:helpers')\n")
            with self.assertRaises(LibraryError):
                f.library.module('shared/text:sneaky')


class PipelineTests(unittest.TestCase):
    def test_resolution_ids_changes_and_diff(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            base = f.pipelines.resolve({'name': 'toy', 'root': f.tree()})
            again = f.pipelines.resolve({'name': 'renamed', 'root': f.tree()})
            self.assertEqual(base['id'], again['id'])
            self.assertEqual(set(base['library']), {'flow:sequence', 'toy:upper', 'toy:count', 'shared/text:helpers'})
            derived = f.pipelines.resolve({'name': 'louder', 'base': base['id'],
                                           'changes': [{'at': 'upper', 'settings': {'suffix': '!'}}]})
            self.assertNotEqual(derived['id'], base['id'])
            self.assertEqual(pipeline_diff(base, derived),
                             [{'path': 'upper', 'change': 'changed', 'settings': [{'suffix': ''}, {'suffix': '!'}]}])
            removed = f.pipelines.resolve({'base': base['id'], 'changes': [{'at': 'count', 'remove': True}]})
            self.assertEqual([c['name'] for c in removed['root']['children']], ['upper'])

    def test_invalid_trees_are_refused(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            bad = f.tree()
            bad['children'][0]['settings'] = {'volume': 11}
            bad['children'][1]['prompts'] = {}
            with self.assertRaises(PipelineError) as caught:
                f.pipelines.resolve({'root': bad})
            self.assertIn('no settings volume', str(caught.exception))
            self.assertIn('needs prompts', str(caught.exception))
            wired = f.tree()
            wired['children'][0]['inputs'] = {'input': 'count'}
            with self.assertRaises(PipelineError):
                f.pipelines.resolve({'root': wired})

    def test_edited_entries_are_refused_and_tags_are_a_log(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            base = f.pipelines.resolve({'root': f.tree()})
            f.pipelines.set_tag('production', base['id'], note='first')
            self.assertEqual(f.pipelines.load('production')['id'], base['id'])
            (f.root/'steps/toy/upper.py').write_text(UPPER+'\n# edited\n')
            self.assertEqual(len(f.pipelines.verify(base)), 1)
            with self.assertRaises(PipelineError):
                f.pipelines.build(base)
            self.assertEqual([e['tag'] for e in f.pipelines.tag_log()], ['production'])


class RunnerTests(unittest.TestCase):
    def test_runs_save_outputs_traces_and_continue(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            pipeline = f.pipelines.resolve({'root': f.tree()})
            words = f.datasets.write('words', {'a': 'hi', 'b': 'bad', 'c': 'there'})
            record = f.runner.run('first', pipeline['id'], {'input': words.id})
            output = f.datasets.open(record['output'])
            self.assertEqual(output.load(), {'a': '2 characters', 'b': Failure('validation', 'bad item', 'upper'),
                                             'c': '5 characters'})
            self.assertEqual(f.datasets.open(record['outputs']['upper']).kind, 'shouted')
            self.assertEqual(f.datasets.open(record['traces']).get('a'), [{'step': 'upper', 'event': 'upper'}])
            self.assertEqual(record['items'], {'total': 3, 'failed': {'validation': 1}})
            again = f.runner.run('first', pipeline['id'], {'input': words.id})
            self.assertEqual((again['executions'], again['output']), (2, record['output']))
            with self.assertRaises(RunError):
                f.runner.run('first', pipeline['id'], {'input': words.id}, keys=['a'])
            subset = f.runner.run('subset', pipeline['id'], {'input': words.id}, keys=['a'])
            self.assertEqual(list(f.datasets.open(subset['output']).load()), ['a'])

    def test_runs_report_live_progress_and_state(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            pipeline = f.pipelines.resolve({'root': f.tree()})
            words = f.datasets.write('words', {'a': 'hi', 'b': 'bad'})
            f.runner.run('watched', pipeline['id'], {'input': words.id}, labels={'experiment': 'x'})
            live = f.runner.live('watched')
            self.assertEqual((live['state'], live['running']), ('complete', False))
            self.assertEqual(live['record']['labels'], {'experiment': 'x'})
            steps = {s['path']: s for s in live['progress']['steps']}
            self.assertEqual(set(steps), {'', 'upper', 'count'})
            self.assertEqual(steps['upper']['failed'], {'validation': 1})
            self.assertEqual(live['progress']['items']['a']['step'], 'upper')
            # A run whose process ended while it was running reads as interrupted.
            record = json.loads((f.root/'runs/watched/run.json').read_text())
            (f.root/'runs/watched/run.json').write_text(json.dumps({**record, 'status': 'running'}))
            self.assertEqual(f.runner.live('watched')['state'], 'interrupted')

    def test_a_subtree_runs_from_a_saved_dataset(self):
        with TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            pipeline = f.pipelines.resolve({'root': f.tree()})
            shouted = f.datasets.write('shouted', {'a': 'HELLO'})
            record = f.runner.run('tail', pipeline['id'], {'input': shouted.id}, at='count')
            self.assertEqual(f.datasets.open(record['output']).load(), {'a': '5 characters'})


class Response:
    def __init__(self, body, status=200):
        self.status_code, self.body, self.text = status, body, json.dumps(body)

    def json(self):
        return self.body


def answer(content, reason='stop', usage=None):
    return Response({'choices': [{'finish_reason': reason, 'message': {'content': content}}],
                     'usage': usage or {'prompt_tokens': 1000, 'completion_tokens': 100}})


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'models.json').write_text(json.dumps({'models': {'fake': {
            'provider': 'chat-completions', 'pricing_usd_per_million': {'input': 1.0, 'output': 1.0}}}}))
        self.provider = provider_for('fake', self.root/'models.json')
        self.request = {'model': 'fake', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 1000}
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def models(self, run='r1', replies=None, **options):
        replies = list(replies or [])

        def transport(request):
            self.sent.append(request)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return Models(run, self.root/'runs'/run/'ledger.jsonl', CallStore(self.root/'calls'),
                      catalog_path=self.root/'models.json', transport=transport, **options)

    def test_new_calls_are_capped_charged_and_then_reused(self):
        models = self.models(replies=[answer('{"ok": 1}')], mode='send', limit=1.0)
        value, key, kind = models.call(self.provider, self.request)
        self.assertEqual((value, kind), ({'ok': 1}, 'new'))
        self.assertAlmostEqual(models.spent(), 1100/1_000_000)
        value, _, kind = self.models('r2', mode='cached').call(self.provider, self.request)
        self.assertEqual((value, kind), ({'ok': 1}, 'reused'))
        with self.assertRaises(BudgetRefused):
            self.models('r3', mode='send', limit=0.0).call(self.provider, {**self.request, 'max_tokens': 5})
        with self.assertRaises(NotSent):
            self.models('r4', mode='dry').call(self.provider, {**self.request, 'max_tokens': 6})
        self.assertEqual(len(self.sent), 1)

    def test_peak_hours_double_prices_on_weekday_windows_only(self):
        from datetime import datetime, timezone
        from court.config import ROOT
        deepseek = provider_for('deepseek-flash', ROOT/'pipelines/models.json').pricing
        at = lambda day, hour, minute=0: datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)
        # October 12, 2026 is a Monday; the 17th and 18th are a weekend.
        self.assertEqual([deepseek.factor(at(12, 1)), deepseek.factor(at(12, 3, 59)), deepseek.factor(at(12, 4)),
                          deepseek.factor(at(14, 6)), deepseek.factor(at(16, 9, 59)), deepseek.factor(at(16, 10)),
                          deepseek.factor(at(17, 2)), deepseek.factor(at(18, 7))], [2, 2, 1, 2, 2, 1, 1, 1])
        self.assertAlmostEqual(deepseek.cost(1_000_000, 0, 0, at(12, 2)), 0.30)
        self.assertAlmostEqual(deepseek.cost(1_000_000, 0, 0, at(12, 12)), 0.15)

    def test_a_call_is_charged_at_the_rates_when_it_was_sent(self):
        from datetime import datetime, timezone
        (self.root/'models.json').write_text(json.dumps({'models': {'fake': {
            'provider': 'chat-completions', 'pricing': {
                'function': 'peak-hours', 'usd_per_million': {'input': 1.0, 'output': 1.0},
                'peak': {'multiplier': 3, 'weekdays': ['mon'], 'hours': [['00:00', '24:00']]}}}}}))
        provider = provider_for('fake', self.root/'models.json')
        monday = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
        models = self.models(replies=[answer('{"ok": 1}')], mode='send', limit=1.0)
        models.clock = lambda: monday
        models.call(provider, self.request)
        self.assertAlmostEqual(models.spent(), 3*1100/1_000_000)

    def test_the_ledger_summary_has_spending_tokens_and_steps(self):
        from court.core.models import ledger_summary
        models = self.models(replies=[answer('{"ok": 1}', usage={'prompt_tokens': 1000, 'prompt_cache_hit_tokens': 400,
                                                                 'completion_tokens': 100})], mode='send', limit=1.0)
        models.call(self.provider, self.request, step='extract/triage')
        models.call(self.provider, self.request, step='extract/triage')
        summary = ledger_summary(models.entries)
        self.assertEqual(summary['calls'], {'reused': 1, 'sent': 1, 'failed': 0, 'would_send': 0})
        self.assertEqual(summary['tokens'], {'input': 1000, 'cached': 400, 'output': 100})
        self.assertAlmostEqual(summary['spent_usd'], models.spent())
        self.assertEqual([s['step'] for s in summary['steps']], ['extract/triage'])
        self.assertEqual(len(summary['timeline']), 1)

    def test_the_ledger_summary_separates_charges_from_calls_in_flight(self):
        from court.core.models import ledger_summary
        entries = [{'event': 'sent', 'key': 'a', 'attempt': '1', 'usd': 0.05, 'step': 'extract/triage', 'items': ['g-1'],
                    'at': '2026-10-12T00:00:00+00:00'},
                   {'event': 'charged', 'key': 'a', 'attempt': '1', 'usd': 0.01, 'status': 'ok', 'step': 'extract/triage',
                    'at': '2026-10-12T00:00:10+00:00'},
                   {'event': 'sent', 'key': 'b', 'attempt': '2', 'usd': 0.04, 'step': 'extract/triage', 'items': ['g-2'],
                    'at': '2026-10-12T00:00:11+00:00'}]
        summary = ledger_summary(entries)
        self.assertAlmostEqual(summary['charged_usd'], 0.01)
        self.assertAlmostEqual(summary['outstanding_usd'], 0.04)
        self.assertAlmostEqual(summary['spent_usd'], 0.05)
        self.assertEqual([(c['step'], c['items']) for c in summary['in_flight']], [('extract/triage', ['g-2'])])
        step = summary['steps'][0]
        self.assertEqual((step['charged_usd'], step['outstanding_usd'], step['in_flight']), (0.01, 0.04, 1))

    def test_failed_deliveries_are_resent_only_when_allowed_and_once_per_run(self):
        self.models(replies=[answer('{"x"', reason='length')], mode='send', limit=1.0)
        with self.assertRaises(DeliveryFailed):
            self.models(replies=[answer('{"x"', reason='length')], mode='send', limit=1.0).call(self.provider, self.request)
        with self.assertRaises(DeliveryFailed):
            self.models('r1', mode='send', limit=1.0).call(self.provider, self.request)
        resend = self.models('r2', replies=[answer('{"x"', reason='length')], mode='send', limit=1.0,
                             resend_failed=True)
        with self.assertRaises(DeliveryFailed):
            resend.call(self.provider, self.request)
        # The same run, resumed, does not resend the same call again.
        with self.assertRaises(DeliveryFailed):
            self.models('r2', mode='send', limit=1.0, resend_failed=True).call(self.provider, self.request)
        self.assertEqual(len(self.sent), 2)
        # Each run is charged for its own attempt.
        self.assertGreater(self.models('r1').spent(), 0)
        self.assertGreater(self.models('r2').spent(), 0)

    def test_rejected_answers_try_further_samples_within_the_limit(self):
        models = self.models(replies=[answer('{"n": 1}'), answer('{"n": 2}')], mode='send', limit=1.0, resamples=1)

        def accept(value):
            if value['n'] < 2:
                raise ValueError('too small')
            return value['n']
        attempts = []
        accepted, _, _ = models.ask(self.provider, self.request, accept, lambda *a: attempts.append(a))
        self.assertEqual(accepted, 2)
        self.assertEqual([a[2] for a in attempts], ['too small', None])
        # Resumed, both samples are reused; nothing is paid again.
        again = self.models(mode='send', limit=1.0, resamples=1)
        self.assertEqual(again.ask(self.provider, self.request, accept)[0], 2)
        self.assertEqual(len(self.sent), 2)
        with self.assertRaises(Rejected):
            self.models('r9', mode='cached').ask(self.provider, self.request, accept)

    def test_invalid_json_is_repaired_by_a_separate_call(self):
        models = self.models(replies=[answer('not json at all'), answer('{"fixed": true}')], mode='send', limit=1.0)
        value, _, kind = models.call(self.provider, self.request)
        self.assertEqual((value, kind), ({'fixed': True}, 'new'))
        self.assertEqual(len(self.sent), 2)


if __name__ == '__main__':
    unittest.main()
