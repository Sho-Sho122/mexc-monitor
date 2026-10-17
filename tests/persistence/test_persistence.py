"""Real Git tests. All origins are disposable LOCAL bare repositories."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

import research_persistence as p


class LocalGit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.top = Path(self.temp.name)
        self.bare = self.top / 'remote.git'
        self.environment = patch.dict(os.environ, {
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
            'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
            'GIT_TERMINAL_PROMPT': '0',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.g(self.top, 'init', '--bare', '--initial-branch=main', str(self.bare))
        seed = self.top / 'seed'
        self.g(self.top, 'init', '--initial-branch=main', str(seed))
        self.put(seed, '511_data/forward/state.json', 'zero')
        self.put(seed, 'strategy_pullback.csv', 'initial\n')
        self.put(seed, 'monitor.json', 'monitor0')
        self.put(seed, 'logic.py', '# frozen research logic\n')
        self.g(seed, 'add', '.')
        self.g(seed, 'commit', '-m', 'fixture baseline')
        self.g(seed, 'remote', 'add', 'origin', str(self.bare))
        self.g(seed, 'push', 'origin', 'main')
        self.worker = self.clone('worker')
        self.backup = self.top / 'recovery'
        self.counter = 0

    def g(self, root, *args):
        result = subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def put(self, root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    def clone(self, name):
        root = self.top / name
        self.g(self.top, 'clone', '--no-hardlinks', str(self.bare), str(root))
        self.assertEqual(self.g(root, 'remote', 'get-url', 'origin'), str(self.bare))
        return root

    def advance(self, name, value):
        self.counter += 1
        root = self.clone('other' + str(self.counter))
        self.put(root, name, value)
        self.g(root, 'add', name)
        self.g(root, 'commit', '-m', 'independent writer')
        self.g(root, 'push', 'origin', 'main')
        return self.g(root, 'rev-parse', 'HEAD')

    def read_remote(self, name):
        return self.g(self.bare, 'show', 'main:' + name)

    def ready(self, group='511', value='computed', worker=None, backup=None):
        worker = worker or self.worker
        backup = backup or self.backup
        p.prepare(worker, backup, delay=0)
        self.put(worker, '511_data/forward/state.json' if group == '511' else 'strategy_pullback.csv', value)
        p.archive(worker, backup, group)

    def audit(self, group='511', backup=None):
        path = (backup or self.backup) / group / 'save_audit.jsonl'
        return [json.loads(x) for x in path.read_text().splitlines()]

    def test_stale_checkout_refreshes_both_groups_before_compute(self):
        old = self.g(self.worker, 'rev-parse', 'HEAD')
        self.advance('strategy_pullback.csv', 'new research')
        new = self.advance('511_data/forward/state.json', 'new state')
        self.assertEqual(p.prepare(self.worker, self.backup, delay=0), new)
        self.assertEqual((self.worker / 'strategy_pullback.csv').read_text(), 'new research')
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'new state')
        self.assertEqual(self.g(self.worker, 'rev-parse', 'HEAD'), old)
        self.put(self.worker, '511_data/forward/state.json', 'computed from new state')
        p.archive(self.worker, self.backup, '511')
        p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'computed from new state')
        self.assertEqual(self.read_remote('strategy_pullback.csv'), 'new research')

    def test_normal_save_preserves_head_index_outputs_and_other_paths(self):
        self.ready()
        head = self.g(self.worker, 'rev-parse', 'HEAD')
        index = (self.worker / '.git/index').read_bytes()
        before = (self.worker / '511_data/forward/state.json').read_bytes()
        p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.g(self.worker, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.worker / '.git/index').read_bytes(), index)
        self.assertEqual((self.worker / '511_data/forward/state.json').read_bytes(), before)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'computed')
        self.assertEqual(self.read_remote('monitor.json'), 'monitor0')
        self.assertEqual(self.read_remote('logic.py'), '# frozen research logic')

    def test_repeated_save_and_restart_do_not_add_commits(self):
        self.ready()
        p.save(self.worker, self.backup, '511', delay=0)
        first = self.g(self.bare, 'rev-parse', 'main')
        restarted = self.clone('restart')
        p.save(restarted, self.backup, '511', delay=0)
        self.assertEqual(self.g(self.bare, 'rev-parse', 'main'), first)
        self.assertEqual(self.audit()[-1]['status'], 'ALREADY_SAVED')

    def test_unrelated_writer_before_save_is_preserved(self):
        self.ready()
        self.advance('monitor.json', 'new monitor')
        p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('monitor.json'), 'new monitor')

    def test_unrelated_writer_between_fetch_and_push_retries(self):
        self.ready()
        original = p.git
        calls = []
        def race(root, *args, **kwargs):
            if args[0] == 'push':
                calls.append(args)
                if len(calls) == 1:
                    self.advance('monitor.json', 'raced monitor')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=race):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.read_remote('monitor.json'), 'raced monitor')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'computed')

    def test_state_conflict_before_save_fails_closed_and_retains_recovery(self):
        self.ready()
        archive = (self.backup / '511/state.tar').read_bytes()
        self.advance('511_data/forward/state.json', 'winner')
        with self.assertRaisesRegex(p.PersistenceError, '511_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'winner')
        self.assertEqual((self.backup / '511/state.tar').read_bytes(), archive)
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'computed')
        self.assertEqual(self.audit()[-1]['status'], 'FAILED')

    def test_state_conflict_during_push_does_not_retry_overwrite(self):
        self.ready()
        original = p.git
        pushes = []
        def race(root, *args, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                if len(pushes) == 1:
                    self.advance('511_data/forward/state.json', 'winner')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=race):
            with self.assertRaisesRegex(p.PersistenceError, '511_REMOTE_CONFLICT'):
                p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'winner')

    def concurrent(self, identical):
        second = self.clone('second')
        backup2 = self.top / 'recovery2'
        self.ready(value='one')
        self.ready(value='one' if identical else 'two', worker=second, backup=backup2)
        barrier = threading.Barrier(2)
        original = p.git
        seen = set()
        lock = threading.Lock()
        def racing(root, *args, **kwargs):
            if args[0] == 'push':
                with lock:
                    first = str(root) not in seen
                    seen.add(str(root))
                if first:
                    barrier.wait(timeout=30)
            return original(root, *args, **kwargs)
        def run(root, backup):
            try:
                p.save(root, backup, '511', delay=0)
                return 'SAVED'
            except p.PersistenceError as exc:
                return str(exc)
        with patch.object(p, 'git', side_effect=racing):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                a = pool.submit(run, self.worker, self.backup)
                b = pool.submit(run, second, backup2)
                outcomes = [a.result(), b.result()]
        self.assertEqual(self.g(self.bare, 'rev-list', '--count', 'main'), '2')
        return outcomes

    def test_real_simultaneous_writers_only_one_different_state_wins(self):
        self.assertCountEqual(self.concurrent(False), ['SAVED', '511_REMOTE_CONFLICT'])
        self.assertIn(self.read_remote('511_data/forward/state.json'), ('one', 'two'))

    def test_real_simultaneous_identical_results_are_idempotent(self):
        self.assertEqual(self.concurrent(True), ['SAVED', 'SAVED'])
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'one')

    def test_fetch_dns_failure_retries_without_losing_outputs(self):
        self.ready()
        original = p.git
        attempts = []
        def network(root, *args, **kwargs):
            if args[0] == 'fetch':
                attempts.append(1)
                if len(attempts) < 3:
                    raise p.GitFailure('fetch', 128, 'could not resolve host')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=network):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(any('DNS' in x.get('error', '') for x in self.audit()))

    def test_exhausted_push_failures_keep_remote_outputs_and_archive(self):
        self.ready()
        original = p.git
        pushes = []
        def network(root, *args, **kwargs):
            if args[0] == 'push':
                pushes.append(1)
                raise p.GitFailure('push', 128, 'HTTP 500')
            return original(root, *args, **kwargs)
        with patch.object(p, 'git', side_effect=network):
            with self.assertRaisesRegex(p.PersistenceError, 'RETRIES_EXHAUSTED'):
                p.save(self.worker, self.backup, '511', attempts=3, delay=0)
        self.assertEqual(len(pushes), 3)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')
        manifest, files = p.payload(self.backup, '511')
        self.assertEqual(files['511_data/forward/state.json'], b'computed')
        self.assertEqual(self.audit()[-1]['status'], 'FAILED')

    def test_accepted_push_with_lost_reply_is_reconciled(self):
        self.ready()
        original = p.git
        pushes = []
        def lost(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == 'push':
                pushes.append(1)
                raise p.GitFailure('push', 128, 'timed out')
            return result
        with patch.object(p, 'git', side_effect=lost):
            p.save(self.worker, self.backup, '511', attempts=1, delay=0)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.g(self.bare, 'rev-list', '--count', 'main'), '2')
        self.assertEqual(self.audit()[-1]['status'], 'ALREADY_SAVED')

    def test_research_saves_even_when_511_archive_fails(self):
        p.prepare(self.worker, self.backup, delay=0)
        self.put(self.worker, 'strategy_pullback.csv', 'research output')
        self.put(self.worker, '511_data/forward/state.json', '511 output')
        p.archive(self.worker, self.backup, 'research')
        original = p.tarfile.open
        def failure(path, *args, **kwargs):
            if '511' in Path(path).parts:
                raise OSError('fixture disk full')
            return original(path, *args, **kwargs)
        with patch.object(p.tarfile, 'open', side_effect=failure):
            with self.assertRaises(OSError):
                p.archive(self.worker, self.backup, '511')
        p.save(self.worker, self.backup, 'research', delay=0)
        self.assertEqual(self.read_remote('strategy_pullback.csv'), 'research output')
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), '511 output')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')

    def test_research_save_success_survives_511_conflict(self):
        p.prepare(self.worker, self.backup, delay=0)
        self.put(self.worker, 'strategy_pullback.csv', 'research output')
        self.put(self.worker, '511_data/forward/state.json', '511 output')
        for group in p.GROUPS:
            p.archive(self.worker, self.backup, group)
        self.advance('511_data/forward/state.json', 'winner')
        p.save(self.worker, self.backup, 'research', delay=0)
        with self.assertRaisesRegex(p.PersistenceError, '511_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('strategy_pullback.csv'), 'research output')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'winner')

    def test_research_state_conflict_is_not_blindly_overwritten(self):
        self.ready(group='research')
        self.advance('strategy_pullback.csv', 'winner research')
        with self.assertRaisesRegex(p.PersistenceError, 'RESEARCH_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, 'research', delay=0)
        self.assertEqual(self.read_remote('strategy_pullback.csv'), 'winner research')

    def test_dirty_input_is_refused_without_overwrite(self):
        self.put(self.worker, '511_data/forward/state.json', 'unsaved')
        with self.assertRaisesRegex(p.PersistenceError, 'DIRTY_INPUT'):
            p.prepare(self.worker, self.backup, delay=0)
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'unsaved')

    def test_prepare_fetch_failure_leaves_no_prepared_marker(self):
        with patch.object(p, 'latest', side_effect=p.GitFailure('fetch', 128, 'could not resolve')):
            with self.assertRaises(p.GitFailure):
                p.prepare(self.worker, self.backup, attempts=2, delay=0)
        self.assertFalse((self.backup / 'baseline.json').exists())
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'zero')

    def test_interrupted_input_refresh_cannot_be_certified_or_archived(self):
        self.advance('511_data/forward/state.json', 'latest')
        original = p.atomic
        def interrupted(path, data):
            if path.name == 'baseline.json':
                raise OSError('interrupted before completion marker')
            return original(path, data)
        with patch.object(p, 'atomic', side_effect=interrupted):
            with self.assertRaises(OSError):
                p.prepare(self.worker, self.backup, delay=0)
        with self.assertRaises(FileNotFoundError):
            p.archive(self.worker, self.backup, '511')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'latest')

    def test_code_changed_before_compute_requires_new_workflow(self):
        self.advance('logic.py', '# new code')
        with self.assertRaisesRegex(p.PersistenceError, 'CODE_CHANGED'):
            p.prepare(self.worker, self.backup, delay=0)
        self.assertFalse((self.backup / 'baseline.json').exists())

    def test_code_changed_after_compute_is_not_mixed_with_old_outputs(self):
        self.ready()
        self.advance('logic.py', '# new code')
        with self.assertRaisesRegex(p.PersistenceError, 'CODE_CHANGED_DURING_SAVE'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')

    def test_corrupted_recovery_cannot_push(self):
        self.ready()
        with (self.backup / '511/state.tar').open('ab') as f:
            f.write(b'corruption')
        with self.assertRaisesRegex(p.PersistenceError, 'ARCHIVE_HASH_MISMATCH'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.g(self.bare, 'rev-list', '--count', 'main'), '1')

    def test_traversal_member_is_refused_even_with_updated_tar_hash(self):
        self.ready()
        target = self.backup / '511'
        with tarfile.open(target / 'state.tar', 'w') as t:
            member = tarfile.TarInfo('../outside')
            member.size = 1
            t.addfile(member, io.BytesIO(b'x'))
        manifest = json.loads((target / 'manifest.json').read_text())
        manifest['archive_sha256'] = p.sha((target / 'state.tar').read_bytes())
        p.write_json(target / 'manifest.json', manifest)
        with self.assertRaisesRegex(p.PersistenceError, 'UNSAFE_ARCHIVE_MEMBER'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertFalse((self.top / 'outside').exists())

    def test_repeated_archive_does_not_replace_first_evidence(self):
        self.ready()
        first = (self.backup / '511/state.tar').read_bytes()
        self.put(self.worker, '511_data/forward/state.json', 'different')
        with self.assertRaisesRegex(p.PersistenceError, 'ARCHIVE_ALREADY_EXISTS'):
            p.archive(self.worker, self.backup, '511')
        self.assertEqual((self.backup / '511/state.tar').read_bytes(), first)

    def test_cli_return_codes_and_recovery_after_conflict(self):
        script = str(Path(p.__file__).resolve())
        def cli(action):
            args = [sys.executable, script, action, '--backup', str(self.backup)]
            if action != 'prepare':
                args += ['--group', '511']
            return subprocess.run(args, cwd=self.worker, capture_output=True, timeout=45)
        self.assertEqual(cli('prepare').returncode, 0)
        self.put(self.worker, '511_data/forward/state.json', 'cli output')
        self.assertEqual(cli('archive').returncode, 0)
        self.advance('511_data/forward/state.json', 'winner')
        result = cli('save')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'511_REMOTE_CONFLICT', result.stderr)
        self.assertEqual(p.payload(self.backup, '511')[1]['511_data/forward/state.json'], b'cli output')

    def test_new_remote_data_file_is_also_a_conflict(self):
        self.ready()
        self.advance('511_data/forward/additional.csv', 'concurrent evidence')
        with self.assertRaisesRegex(p.PersistenceError, '511_REMOTE_CONFLICT'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/additional.csv'), 'concurrent evidence')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')

    def test_save_uses_verified_archive_not_later_working_file_changes(self):
        self.ready()
        self.put(self.worker, '511_data/forward/state.json', 'later local mutation')
        p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'computed')
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'later local mutation')

    def test_missing_output_is_not_interpreted_as_deletion(self):
        p.prepare(self.worker, self.backup, delay=0)
        (self.worker / '511_data/forward/state.json').unlink()
        with self.assertRaisesRegex(p.PersistenceError, 'OUTPUT_FILE_MISSING'):
            p.archive(self.worker, self.backup, '511')
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')
        self.assertFalse((self.backup / '511/manifest.json').exists())

    def test_remote_input_removal_requires_review(self):
        other = self.clone('deleting_writer')
        self.g(other, 'rm', '511_data/forward/state.json')
        self.g(other, 'commit', '-m', 'fixture remote removal')
        self.g(other, 'push', 'origin', 'main')
        with self.assertRaisesRegex(p.PersistenceError, 'REMOTE_INPUT_REMOVED'):
            p.prepare(self.worker, self.backup, delay=0)
        self.assertEqual((self.worker / '511_data/forward/state.json').read_text(), 'zero')

    def test_unknown_manifest_version_is_blocked(self):
        self.ready()
        path = self.backup / '511/manifest.json'
        manifest = json.loads(path.read_text())
        manifest['version'] = 999
        p.write_json(path, manifest)
        with self.assertRaisesRegex(p.PersistenceError, 'INVALID_MANIFEST'):
            p.save(self.worker, self.backup, '511', delay=0)
        self.assertEqual(self.read_remote('511_data/forward/state.json'), 'zero')


class Workflow(unittest.TestCase):
    def test_failure_isolation_and_recovery_wiring(self):
        import yaml
        path = Path(__file__).resolve().parents[2] / '.github/workflows/research.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        steps = workflow['jobs']['scan']['steps']
        byid = {s['id']: s for s in steps if 'id' in s}
        legacy = byid['research_save']
        self.assertNotIn('511', legacy['if'])
        self.assertNotIn('recovery', legacy['if'])
        self.assertEqual(legacy['continue-on-error'], 'true')
        self.assertIn("steps.model511.outcome == 'success'", byid['save511']['if'])
        self.assertIn("steps.research_save.outcome == 'success'", byid['save511']['if'])
        for name in ('archive511', 'recovery511', 'save511', 'audit511'):
            self.assertEqual(byid[name]['continue-on-error'], 'true')
        self.assertIn('511_data', byid['audit511']['with']['path'])
        self.assertIn('research_exit_results*.csv', byid['audit_research']['with']['path'])
        self.assertIn('always()', byid['audit_research']['if'])
        self.assertLess(steps.index(byid['prepare_state']), steps.index(byid['research_start']))
        self.assertLess(steps.index(byid['recovery511']), steps.index(legacy))
        self.assertLess(steps.index(byid['recovery_research']), steps.index(legacy))
        self.assertEqual(workflow['concurrency']['group'], 'mexc-research')
        self.assertEqual(workflow['concurrency']['cancel-in-progress'], 'false')
        self.assertEqual(workflow['permissions'], {'contents': 'write'})
        self.assertEqual(list(workflow['on']), ['workflow_dispatch'])
        self.assertNotIn('reset --hard', path.read_text())


if __name__ == '__main__':
    unittest.main()
