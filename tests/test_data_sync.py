"""Exercise the migration script with real rsync and a local SSH stand-in."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('rsync') and shutil.which('git'), 'Requires rsync and git')
class DataSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source, self.destination = base/'source', base/'destination'
        (self.source/'scripts').mkdir(parents=True)
        script = Path(__file__).resolve().parents[1]/'scripts/sync-pipeline-data.sh'
        shutil.copyfile(script, self.source/'scripts'/script.name)
        (self.source/'.gitignore').write_text('in/\ndata/\nout/\n.env\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Migration test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'fixture')
        subprocess.run(['git', 'clone', '-q', str(self.source), str(self.destination)], check=True)
        for directory in ('in', 'data', 'out'):
            (self.source/directory).mkdir()
            file = self.source/directory/'saved.txt'
            file.write_text(directory+' saved work\n')
            os.utime(file, ns=(1700000000123456789, 1700000000123456789))
        (self.source/'.env').write_text('SECRET=not-for-transfer\n')
        bin_dir = base/'bin'
        bin_dir.mkdir()
        # rsync's protocol still runs over a subprocess; only network transport
        # is replaced so these tests never contact an actual SSH host.
        ssh = bin_dir/'ssh'
        ssh.write_text('#!/bin/sh\nif [ "$1" = -o ]; then shift 2; fi\nshift\nexec /bin/sh -c "$*"\n')
        ssh.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(bin_dir)+os.pathsep+os.environ['PATH']}

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.source), '-c', 'commit.gpgsign=false',
                                 '-c', 'core.hooksPath=/dev/null', *args], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def run_sync(self, *options, destination=None):
        return subprocess.run(['bash', str(self.source/'scripts/sync-pipeline-data.sh'),
                               *options, 'migration-test', str(destination or self.destination)],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def test_preview_leaves_destination_data_absent(self):
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.destination/'data').exists())

    def test_apply_preserves_saved_files_and_timestamps_without_credentials(self):
        result = self.run_sync('--apply', '--writers-stopped')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified:', result.stdout)
        for directory in ('in', 'data', 'out'):
            file = self.destination/directory/'saved.txt'
            self.assertEqual(file.read_bytes(), (self.source/directory/'saved.txt').read_bytes())
            self.assertEqual(file.stat().st_mtime_ns, 1700000000123456789)
        self.assertFalse((self.destination/'.env').exists())

    def test_apply_requires_quiescence_and_refuses_existing_data(self):
        result = self.run_sync('--apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--writers-stopped', result.stderr)
        (self.destination/'data').mkdir()
        (self.destination/'data/remote-only.txt').write_text('preserve me')
        result = self.run_sync('--apply', '--writers-stopped')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not empty', result.stderr)
        result = self.run_sync('--apply', '--writers-stopped', '--resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination/'data/remote-only.txt').read_text(), 'preserve me')

    def test_resume_repairs_same_size_same_timestamp_corruption(self):
        self.assertEqual(self.run_sync('--apply', '--writers-stopped').returncode, 0)
        file = self.destination/'data/saved.txt'
        file.write_bytes(b'x'*file.stat().st_size)
        os.utime(file, ns=(1700000000123456789, 1700000000123456789))
        result = self.run_sync('--apply', '--writers-stopped', '--resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(file.read_bytes(), (self.source/'data/saved.txt').read_bytes())

    def test_rejects_different_commit_dirty_source_and_shell_paths(self):
        (self.source/'new-code.txt').write_text('new revision')
        result = self.run_sync()
        self.assertIn('Commit or resolve', result.stderr)
        self.git('add', 'new-code.txt')
        self.git('-c', 'user.name=Migration test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'next revision')
        self.assertIn('Destination commit differs', self.run_sync().stderr)
        self.assertIn('simple absolute', self.run_sync(destination='/tmp/unsafe;command').stderr)
        self.assertIn('Unsafe destination', self.run_sync(destination='/tmp/../').stderr)
