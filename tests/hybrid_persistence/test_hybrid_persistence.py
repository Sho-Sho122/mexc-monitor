"""No network: all writes use disposable local bare Git repositories."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import hybrid_persistence as p


class HybridPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
            'GIT_TERMINAL_PROMPT': '0', 'GITHUB_REPOSITORY': 'fixture/local',
            'GITHUB_RUN_ID': '101', 'GITHUB_RUN_ATTEMPT': '1',
        })
        env.start()
        self.addCleanup(env.stop)
        self.remote = self.top/'remote.git'
        self.g(self.top, 'init', '--bare', '--initial-branch=main', str(self.remote))
        seed = self.top/'seed'
        self.g(self.top, 'init', '--initial-branch=main', str(seed))
        self.put(seed, p.STATE, b'{"n":0}')
        self.put(seed, p.TRADES, b'id,value\n0,initial\n')
        self.put(seed, 'research.csv', b'original research\n')
        self.put(seed, '511_data/forward/state.json', b'{"frozen":true}')
        self.put(seed, 'mexc_hybrid_system.py', b"from pathlib import Path\nPath('mexc_hybrid_state.json').write_bytes(b'{\"n\":1}')\nwith Path('mexc_hybrid_trades.csv').open('ab') as f: f.write(b'1,new\\n')\n")
        self.g(seed, 'add', '.')
        self.g(seed, 'commit', '-m', 'fixture')
        self.g(seed, 'remote', 'add', 'origin', self.remote.as_uri())
        self.g(seed, 'push', 'origin', 'main')
        self.worker = self.clone('worker')
        self.backup = self.top/'backup'
        self.sequence = 0

    def g(self, root, *args):
        return p.core.git(root, *args).decode().strip()

    def put(self, root, name, data):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def clone(self, name):
        path = self.top/name
        self.g(self.top, 'clone', '--depth=1', self.remote.as_uri(), str(path))
        self.assertEqual(self.g(path, 'rev-parse', '--is-shallow-repository'), 'true')
        return path

    def advance(self, name, data):
        self.sequence += 1
        other = self.clone('other'+str(self.sequence))
        self.put(other, name, data)
        self.g(other, 'add', name)
        self.g(other, 'commit', '-m', 'other writer')
        self.g(other, 'push', 'origin', 'main')

    def remote_bytes(self, name):
        return p.core.git(self.remote, 'show', 'main:'+name)

    def ready(self, worker=None, backup=None):
        worker, backup = worker or self.worker, backup or self.backup
        p.prepare(worker, backup, delay=0)
        self.put(worker, 'mexc_scan_fixture.csv', b'symbol,price\nFIXTURE,10\n')
        p.checkpoint(worker, backup)
        p.execute(worker, backup)
        p.archive(worker, backup)
        self.confirm(worker, backup)

    def artifact(self, backup):
        raw = (backup/'recovery.json').read_bytes()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('recovery.json', raw)
        zipped = stream.getvalue()
        digest = p.core.sha(zipped)
        return {'id': 1, 'expired': False, 'name': 'hybrid-recovery-101-1',
                'workflow_run': {'id': 101}, 'digest': 'sha256:'+digest,
                'expires_at': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}, zipped

    def confirm(self, worker, backup):
        metadata, zipped = self.artifact(backup)
        with patch.object(p, 'remote_artifact', return_value=(metadata, zipped)):
            p.confirm(worker, backup, '1', metadata['digest'])

    def test_normal_save_exact_bytes_and_no_unrelated_change(self):
        head = self.g(self.worker, 'rev-parse', 'HEAD')
        self.ready()
        result = p.save(self.worker, self.backup, delay=0)
        self.assertEqual(result, self.g(self.remote, 'rev-parse', 'main'))
        for name, raw in p.local(self.worker).items():
            self.assertEqual(self.remote_bytes(name), raw)
        self.assertEqual(self.remote_bytes('research.csv'), b'original research\n')
        self.assertEqual(self.remote_bytes('511_data/forward/state.json'), b'{"frozen":true}')
        self.assertEqual(self.g(self.worker, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.g(self.worker, 'diff', '--cached'), '')

    def test_latest_inputs_refreshed_without_checkout_reset(self):
        self.advance(p.STATE, b'{"n":99}')
        p.prepare(self.worker, self.backup, delay=0)
        self.assertEqual((self.worker/p.STATE).read_bytes(), b'{"n":99}')
        self.assertEqual((self.worker/'research.csv').read_bytes(), b'original research\n')

    def test_dirty_inputs_refused(self):
        self.put(self.worker, p.STATE, b'{}')
        with self.assertRaisesRegex(p.Error, 'DIRTY_INPUT'):
            p.prepare(self.worker, self.backup, delay=0)

    def test_invalid_remote_state_refused_without_refresh(self):
        original = p.local(self.worker)
        self.advance(p.STATE, b'invalid')
        with self.assertRaises(ValueError):
            p.prepare(self.worker, self.backup, delay=0)
        self.assertEqual(original, p.local(self.worker))
        self.assertFalse((self.backup/'baseline.json').exists())

    def test_other_owner_change_preserved(self):
        self.ready()
        self.advance('research.csv', b'new research\n')
        p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes('research.csv'), b'new research\n')

    def test_same_owner_conflict_retains_recovery(self):
        self.ready()
        raw = (self.backup/'recovery.json').read_bytes()
        self.advance(p.STATE, b'{"n":200}')
        with self.assertRaisesRegex(p.Error, 'HYBRID_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":200}')
        self.assertEqual((self.backup/'recovery.json').read_bytes(), raw)

    def test_push_race_retry_preserves_other_writer(self):
        self.ready()
        original = p.git
        raced = []
        def git(root, *args, **kwargs):
            if args[0] == 'push' and not raced:
                raced.append(True)
                self.advance('research.csv', b'raced\n')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=git):
            p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes('research.csv'), b'raced\n')

    def test_same_owner_push_race_rejected(self):
        self.ready()
        original = p.git
        raced = []
        def git(root, *args, **kwargs):
            if args[0] == 'push' and not raced:
                raced.append(True)
                self.advance(p.STATE, b'{"n":300}')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=git), self.assertRaisesRegex(p.Error, 'HYBRID_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":300}')

    def test_push_failure_retains_output_and_remote_unchanged(self):
        self.ready()
        original = p.git
        before = self.g(self.remote, 'rev-parse', 'main')
        def git(root, *args, **kwargs):
            if args[0] == 'push':
                raise p.core.GitFailure('push', 1, 'timed out')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=git), self.assertRaisesRegex(p.Error, 'RETRIES_EXHAUSTED'):
            p.save(self.worker, self.backup, attempts=2, delay=0)
        self.assertEqual(self.g(self.remote, 'rev-parse', 'main'), before)
        self.assertTrue((self.backup/'recovery.json').exists())

    def test_lost_push_response_reconciled_without_duplicate(self):
        self.ready()
        original = p.git
        pushes = []
        def git(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == 'push':
                pushes.append(True)
                raise p.core.GitFailure('push', 1, 'timed out')
            return result
        with patch.object(p, 'git', side_effect=git):
            first = p.save(self.worker, self.backup, delay=0)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(first, p.save(self.worker, self.backup, delay=0))

    def test_recovery_in_fresh_shallow_clone_without_old_commit(self):
        self.ready()
        recovery = (self.backup/'recovery.json').read_bytes()
        old = p.load(self.backup/'baseline.json')['base']
        self.advance('research.csv', b'advanced\n')
        fresh = self.clone('fresh')
        with self.assertRaises(p.core.GitFailure):
            p.core.git(fresh, 'cat-file', '-e', old)
        recovered = self.top/'downloaded'
        self.put(recovered, 'recovery.json', recovery)
        self.confirm(fresh, recovered)
        p.save(fresh, recovered, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":1}')
        self.assertEqual(self.remote_bytes('research.csv'), b'advanced\n')

    def test_process_abrupt_exit_before_push_then_restart(self):
        self.ready()
        code = "import os; from pathlib import Path; import hybrid_persistence as p; original=p.git\ndef git(root,*args,**kwargs):\n if args[0]=='push': os._exit(17)\n return original(root,*args,**kwargs)\np.git=git\np.save(Path(%r),Path(%r),delay=0)" % (str(self.worker), str(self.backup))
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":0}')
        p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":1}')

    def test_no_output_after_runner_loss_cannot_save(self):
        p.prepare(self.worker, self.backup, delay=0)
        self.put(self.worker, 'mexc_scan_fixture.csv', b'symbol,price\nFIXTURE,10\n')
        p.checkpoint(self.worker, self.backup)
        with self.assertRaises(FileNotFoundError):
            p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":0}')
        self.assertFalse(p.load(self.backup/'input-checkpoint.json')['replay_allowed'])

    def test_uncompleted_outputs_not_archived(self):
        p.prepare(self.worker, self.backup, delay=0)
        with self.assertRaises(FileNotFoundError):
            p.archive(self.worker, self.backup)

    def test_no_verified_receipt_no_save(self):
        self.ready()
        (self.backup/'durable_receipt.json').unlink()
        with self.assertRaises(FileNotFoundError):
            p.save(self.worker, self.backup, delay=0)

    def test_receipt_cannot_authorize_modified_bundle(self):
        self.ready()
        with (self.backup/'recovery.json').open('ab') as f:
            f.write(b' ')
        with self.assertRaisesRegex(p.Error, 'DURABILITY_BINDING_MISMATCH'):
            p.save(self.worker, self.backup, delay=0)

    def test_artifact_wrong_run_digest_expiry_payload_blocked(self):
        self.ready()
        metadata, zipped = self.artifact(self.backup)
        cases = [dict(metadata, expired=True), dict(metadata, workflow_run={'id': 999}),
                 dict(metadata, name='hybrid-recovery-101-2'), dict(metadata, digest='sha256:'+'0'*64)]
        for altered in cases:
            with self.subTest(metadata=altered), patch.object(p, 'remote_artifact', return_value=(altered, zipped)), self.assertRaises(p.Error):
                p.confirm(self.worker, self.backup, '1', metadata['digest'])
        with patch.object(p, 'remote_artifact', return_value=(metadata, zipped+b'bad')), self.assertRaises(p.Error):
            p.confirm(self.worker, self.backup, '1', metadata['digest'])

    def test_expired_receipt_blocks(self):
        self.ready()
        receipt = p.load(self.backup/'durable_receipt.json')
        receipt['expires_at'] = '2000-01-01T00:00:00+00:00'
        p.core.atomic(self.backup/'durable_receipt.json', p.canonical(receipt))
        with self.assertRaisesRegex(p.Error, 'EXPIRED'):
            p.save(self.worker, self.backup, delay=0)

    def test_code_change_blocks_without_data_change(self):
        self.ready()
        self.advance('new_logic.py', b'# new\n')
        with self.assertRaisesRegex(p.Error, 'CODE_CHANGED'):
            p.save(self.worker, self.backup, delay=0)
        self.assertEqual(self.remote_bytes(p.STATE), b'{"n":0}')

    def test_concurrent_identical_outputs_are_idempotent(self):
        self.ready()
        second = self.clone('second')
        backup2 = self.top/'backup2'
        self.ready(second, backup2)
        barrier = threading.Barrier(2)
        original = p.candidate
        def candidate(*args, **kwargs):
            commit = original(*args, **kwargs)
            barrier.wait(timeout=30)
            return commit
        with patch.object(p, 'candidate', side_effect=candidate), concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(p.save, w, b, delay=0) for w,b in [(self.worker,self.backup),(second,backup2)]]
            results = [f.result(timeout=60) for f in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.remote_bytes(p.TRADES), b'id,value\n0,initial\n1,new\n')

    def test_repeated_computation_refused(self):
        self.ready()
        with self.assertRaisesRegex(p.Error, 'ALREADY_STARTED'):
            p.execute(self.worker, self.backup)

    def test_concurrent_different_outputs_one_wins_other_recovers(self):
        self.ready()
        second = self.clone('second')
        backup2 = self.top/'backup2'
        self.ready(second, backup2)
        # Model a different completed computation, not a recovery-time rewrite.
        self.put(second, p.STATE, b'{"n":2}')
        completed = p.load(backup2/'completed.json')
        completed['files'] = p.encode(p.local(second))
        p.core.atomic(backup2/'completed.json', p.canonical(completed))
        (backup2/'recovery.json').unlink()
        p.archive(second, backup2)
        self.confirm(second, backup2)
        barrier = threading.Barrier(2)
        original = p.candidate
        def candidate(*args, **kwargs):
            commit = original(*args, **kwargs)
            barrier.wait(timeout=30)
            return commit
        with patch.object(p, 'candidate', side_effect=candidate), concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(p.save, w, b, delay=0) for w,b in [(self.worker,self.backup),(second,backup2)]]
            results = []
            for f in futures:
                try:
                    results.append(('success', f.result(timeout=60)))
                except p.Error as exc:
                    results.append(('error', str(exc)))
        self.assertEqual(sum(r[0] == 'success' for r in results), 1)
        self.assertIn(('error', 'HYBRID_REMOTE_CONFLICT'), results)
        for backup in (self.backup, backup2):
            self.assertTrue((backup/'recovery.json').exists())

    def test_failed_computation_cannot_archive_or_reexecute(self):
        p.prepare(self.worker, self.backup, delay=0)
        self.put(self.worker, 'mexc_scan_fixture.csv', b'fixture')
        p.checkpoint(self.worker, self.backup)
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)), self.assertRaisesRegex(p.Error, 'COMPUTATION_FAILED'):
            p.execute(self.worker, self.backup)
        with self.assertRaises(FileNotFoundError):
            p.archive(self.worker, self.backup)
        with self.assertRaisesRegex(p.Error, 'ALREADY_STARTED'):
            p.execute(self.worker, self.backup)

    def test_modified_output_after_completion_refused(self):
        self.ready()
        (self.backup/'recovery.json').unlink()
        self.put(self.worker, p.STATE, b'{}')
        with self.assertRaisesRegex(p.Error, 'NOT_SUCCESSFULLY_COMPLETED'):
            p.archive(self.worker, self.backup)

    def test_csv_history_rewrite_refused(self):
        self.ready()
        (self.backup/'recovery.json').unlink()
        self.put(self.worker, p.TRADES, b'id,value\n9,replaced\n')
        completed = p.load(self.backup/'completed.json')
        completed['files'] = p.encode(p.local(self.worker))
        p.core.atomic(self.backup/'completed.json', p.canonical(completed))
        with self.assertRaisesRegex(p.Error, 'TRADE_PREFIX_CHANGED'):
            p.archive(self.worker, self.backup)

    def test_downloaded_payload_mismatch_rejected_even_valid_zip_digest(self):
        self.ready()
        meta, _ = self.artifact(self.backup)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('recovery.json', b'{}')
        zipped = stream.getvalue()
        meta['digest'] = 'sha256:'+p.core.sha(zipped)
        with patch.object(p, 'remote_artifact', return_value=(meta,zipped)), self.assertRaisesRegex(p.Error, 'CONTENT_MISMATCH'):
            p.confirm(self.worker, self.backup, '1', meta['digest'])

    def test_scanner_changed_after_checkpoint_refused(self):
        p.prepare(self.worker, self.backup, delay=0)
        self.put(self.worker, 'mexc_scan_fixture.csv', b'original')
        p.checkpoint(self.worker, self.backup)
        self.put(self.worker, 'mexc_scan_fixture.csv', b'changed')
        with self.assertRaisesRegex(p.Error, 'SCANNER_CHANGED'):
            p.execute(self.worker, self.backup)

    def test_corrupt_payload_and_unknown_paths(self):
        for value in [{ '../unsafe': {} }, {p.STATE: {'data': 'e30=', 'sha256': 'wrong', 'blob': 'wrong'}}]:
            with self.subTest(value=value), self.assertRaises(p.Error):
                p.decode(value)


if __name__ == '__main__':
    unittest.main()
