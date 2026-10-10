"""The recipe engine rebuilds the production run's model requests exactly.

Runs only where the production run is present. Sending is disabled and the
cap is zero; every reused answer is read through from the saved calls, which
match only byte-identical requests. ``REPLAY_ALL=1`` replays every group.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from appeal_runner import review_batches
from appeals import prepare
from config import ROOT
from pipeline import recipe as recipes
from pipeline.engine import Engine, clients_for

PRODUCTION = ROOT/'data/production-import-UazDoN/data/appeals/full-run'


@unittest.skipUnless((PRODUCTION/'manifest.json').exists(), 'production run not present')
class ProductionReplayTests(unittest.TestCase):
    def test_requests_and_checkpoints_are_reproduced(self):
        manifest = json.loads((PRODUCTION/'manifest.json').read_text())
        groups = manifest['groups'] if os.getenv('REPLAY_ALL') else manifest['groups'][:150]
        # Keep whole review batches, so every batch request has its original members.
        batches = review_batches(PRODUCTION/'calls')
        chosen = {g['group_id'] for g in groups}
        complete = {gid for _, members in batches if set(members) <= chosen for gid in members}
        partial = {gid for _, members in batches if not set(members) <= chosen for gid in members}
        groups = [g for g in manifest['groups'] if g['group_id'] in chosen - partial or g['group_id'] in complete]
        work = Path(tempfile.mkdtemp(prefix='replay-'))
        self.addCleanup(shutil.rmtree, work)
        shutil.copy2(PRODUCTION/'prompts.json', work/'prompts.json')
        (work/'manifest.json').write_text(json.dumps({**manifest, 'groups': groups}))
        (work/'sources').mkdir()
        for g in groups:
            shutil.copy2(PRODUCTION/'sources'/f'{g["group_id"]}.txt', work/'sources')
        with patch('pipeline.providers.Provider.post', side_effect=AssertionError('network send attempted')), \
                patch.dict('os.environ', {'APPEALS_API_BASE_URL': 'http://127.0.0.1:9', 'APPEALS_API_KEY': 'replay'}), \
                contextlib.redirect_stdout(io.StringIO()):
            loaded, prompts = prepare(work)
            recipe = recipes.validate(recipes.for_run(work, loaded, prompts))
            clients = clients_for(work, recipe, 0, inherit=PRODUCTION/'calls')
            engine = Engine(work, recipe, prompts, loaded, clients, workers=8)
            outputs = engine.run_first_pass()
            engine.finish(outputs)
        failed_batches = {gid for name, members in batches
                          if json.loads((PRODUCTION/'calls'/f'{name}.result.json').read_text())['status'] != 'ok'
                          for gid in members}
        for sub in ('first_pass', 'results'):
            for g in groups:
                gid = g['group_id']
                original = json.loads((PRODUCTION/sub/f'{gid}.json').read_text())
                replayed = json.loads((work/sub/f'{gid}.json').read_text())
                if sub == 'results' and gid in failed_batches:
                    # Failed saved calls are never reused; a new call is refused by the zero cap.
                    self.assertIn('spending cap', replayed.get('error', ''), gid)
                    continue
                self.assertEqual(replayed, original, f'{sub}/{gid}')
        self.assertFalse([p for p in (work/'calls').glob('*.result.json')
                          if not json.loads(p.read_text()).get('inherited_from')])


if __name__ == '__main__':
    unittest.main()
