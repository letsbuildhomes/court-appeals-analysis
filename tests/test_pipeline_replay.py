"""The ported legacy pipeline reproduces the production run's results from its saved calls.

Runs only where the production run is present. Sending is disabled and the
cap is zero; every answer comes from production's saved calls through a
content-addressed index, which matches only byte-identical requests. Each
replayed result must equal production's own outputs converted to the output
contract. ``REPLAY_ALL=1`` replays every case.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from appeal_evidence import source_pages
from pipeline import legacy, providers
from pipeline.corpus import PRODUCTION, Corpus
from pipeline.engine import Context, execute
from pipeline.legacy.convert import outcome
from pipeline.store import CallStore, Calls, build_index

REVIEW = re.compile(r'review-[0-9a-f]{16}\Z')


def review_batches(calls):
    """Review batch membership, from the requests production sent."""
    batches = []
    for path in sorted(calls.glob('review-*.request.json')):
        if REVIEW.fullmatch(path.name[:-len('.request.json')]):
            cases = json.loads(json.loads(path.read_text())['messages'][1]['content'])['cases']
            batches.append([c['group'] for c in cases])
    return batches


@unittest.skipUnless((PRODUCTION/'manifest.json').exists(), 'production run not present')
class ProductionReplayTests(unittest.TestCase):
    def test_results_are_reproduced_from_saved_calls(self):
        corpus = Corpus()
        ids = corpus.ids() if os.getenv('REPLAY_ALL') else corpus.ids()[:150]
        # Keep whole review batches, so every batch request has its original members.
        chosen = set(ids)
        for members in review_batches(PRODUCTION/'calls'):
            if chosen & set(members):
                chosen |= set(members)
        cases = corpus.cases(chosen)
        with tempfile.TemporaryDirectory() as temp:
            provider = providers.build('deepseek-flash', {'model': 'deepseek-flash', 'env': 'APPEALS'}, offline=True)
            build_index(temp, 'production', PRODUCTION/'calls', provider)
            calls = Calls(CallStore(temp), run='replay', limit=0.0)
            with patch('pipeline.providers.Provider.post', side_effect=AssertionError('network send attempted')):
                outputs = execute(legacy.build(), cases, Context(calls, workers=8, offline=True))
        self.assertEqual(calls.spent(), 0)
        self.assertFalse([e for e in calls.entries if e['event'] != 'reused'])
        failed = []
        for case in cases:
            saved = json.loads((PRODUCTION/'results'/f'{case.id}.json').read_text())
            expected, expected_status = outcome(case.id, saved, source_pages(case.source))
            result, status, _ = outputs[case.id]
            self.assertEqual(status['state'], expected_status['state'], case.id)
            self.assertEqual(result, expected, case.id)
            if status['state'] == 'failed':
                failed.append(case.id)
                # Failed deliveries are never reused; a new call is refused by the zero cap.
                if 'Incomplete' in json.dumps(saved.get('error')):
                    self.assertEqual(status['errors'][0]['type'], 'budget')
        if os.getenv('REPLAY_ALL'):
            self.assertEqual(len(failed), 10)


if __name__ == '__main__':
    unittest.main()
