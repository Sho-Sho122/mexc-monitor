"""Persistence failure injection: disposable local bare remotes only."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from model511_adaptive import persistence as p, storage
import research_persistence as legacy

spec = importlib.util.spec_from_file_location('persistence_fixture', Path(__file__).parents[1] / 'persistence/test_persistence.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class AdaptivePersistence(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LocalGit('runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root, self.backup = self.f.worker, self.f.backup
        self.state = storage.empty('2026-10-18T00:00:00+09:00', 'FORWARD', 'fixture')

    def ready(self, root=None, backup=None, revision=1):
        root, backup = root or self.root, backup or self.backup
        p.prepare(root, backup, delay=0)
        state = dict(self.state, revision=revision)
        storage.write(root / p.PREFIX, state)
        p.archive(root, backup)
        return state

    def remote_state(self):
        return json.loads(self.f.read_remote(p.PREFIX + 'adaptive_state.json'))

    def test_normal_save_preserves_research_head_index(self):
        state = self.ready()
        head = self.f.g(self.root, 'rev-parse', 'HEAD')
        index = (self.root / '.git/index').read_bytes()
        p.save(self.root, self.backup, delay=0)
        self.assertEqual(self.remote_state(), state)
        self.assertEqual(self.f.read_remote('strategy_pullback.csv'), 'initial')
        self.assertEqual(self.f.read_remote('511_data/forward/state.json'), 'zero')
        self.assertEqual(self.f.g(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.root / '.git/index').read_bytes(), index)

    def test_stale_checkout_refreshes_all_three_groups(self):
        other = self.f.clone('adaptive-writer')
        self.ready(other, self.f.top / 'other-backup')
        p.save(other, self.f.top / 'other-backup', delay=0)
        self.f.advance('strategy_pullback.csv', 'latest research')
        latest = self.f.advance('511_data/forward/state.json', 'latest beta')
        self.assertEqual(p.prepare(self.root, self.backup, delay=0), latest)
        self.assertEqual(json.loads((self.root / p.PREFIX / 'adaptive_state.json').read_bytes())['revision'], 1)
        self.assertEqual((self.root / 'strategy_pullback.csv').read_text(), 'latest research')
        self.assertEqual((self.root / '511_data/forward/state.json').read_text(), 'latest beta')

    def test_different_concurrent_outputs_have_one_winner(self):
        other, backup = self.f.clone('other'), self.f.top / 'other-recovery'
        self.ready(); self.ready(other, backup, revision=2)
        def publish(root, directory):
            try:
                p.save(root, directory, delay=0)
                return 'saved'
            except p.PersistenceError as exc:
                self.assertIn('ADAPTIVE_REMOTE_CONFLICT', str(exc))
                return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda x: publish(*x), [(self.root, self.backup), (other, backup)]))
        self.assertEqual(sorted(results), ['conflict', 'saved'])
        for directory in (self.backup, backup):
            p.payload(directory, 'adaptive')
        self.assertIn(self.remote_state()['revision'], (1, 2))

    def test_identical_concurrent_outputs_and_restart_are_idempotent(self):
        other, backup = self.f.clone('other'), self.f.top / 'other-recovery'
        self.ready(); self.ready(other, backup)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(p.save, r, b, delay=0) for r,b in ((self.root,self.backup),(other,backup))]
            for future in futures: future.result()
        first = self.f.g(self.f.bare, 'rev-parse', 'main')
        p.save(self.f.clone('restart'), self.backup, delay=0)
        self.assertEqual(first, self.f.g(self.f.bare, 'rev-parse', 'main'))
        self.assertEqual(self.f.g(self.f.bare, 'rev-list', '--count', 'main'), '2')

    def test_projection_only_remote_change_conflicts(self):
        self.ready()
        self.f.advance(p.PREFIX + 'research_scan.csv', 'conflicting projection')
        with self.assertRaisesRegex(p.PersistenceError, 'ADAPTIVE_REMOTE_CONFLICT'):
            p.save(self.root, self.backup, delay=0)
        self.assertEqual(self.f.read_remote(p.PREFIX + 'research_scan.csv'), 'conflicting projection')

    def test_unrelated_race_retries_without_loss(self):
        self.ready(); original = p.git; calls = []
        def race(root, *args, **kw):
            if args[0] == 'push':
                calls.append(1)
                if len(calls) == 1: self.f.advance('strategy_pullback.csv', 'raced research')
            return original(root, *args, **kw)
        with patch.object(p, 'git', side_effect=race): p.save(self.root, self.backup, delay=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.f.read_remote('strategy_pullback.csv'), 'raced research')
        self.assertEqual(self.remote_state()['revision'], 1)

    def test_lost_push_reply_is_reconciled(self):
        self.ready(); original = p.git; calls = []
        def lost(root, *args, **kw):
            result = original(root, *args, **kw)
            if args[0] == 'push':
                calls.append(1)
                raise p.GitFailure('push', 1, 'timed out')
            return result
        with patch.object(p, 'git', side_effect=lost): p.save(self.root, self.backup, attempts=1, delay=0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.remote_state()['revision'], 1)

    def test_transport_exhaustion_preserves_verified_recovery(self):
        self.ready(); original = p.git
        def fail(root, *args, **kw):
            if args[0] == 'push': raise p.GitFailure('push', 1)
            return original(root, *args, **kw)
        with patch.object(p, 'git', side_effect=fail):
            with self.assertRaisesRegex(p.PersistenceError, 'RETRIES_EXHAUSTED'):
                p.save(self.root, self.backup, attempts=2, delay=0)
        manifest, files = p.payload(self.backup, 'adaptive')
        p.validate_output(files)
        self.assertEqual(self.f.g(self.f.bare, 'rev-list', '--count', 'main'), '1')
        self.assertIn('FAILED', (self.backup/'adaptive/save_audit.jsonl').read_text())

    def test_partial_prepare_has_no_ready_marker(self):
        self.f.advance('strategy_pullback.csv', 'new source')
        original = p.atomic
        def fail(path, data):
            if path.name == 'strategy_pullback.csv': raise OSError('injected')
            original(path, data)
        with patch.object(p, 'atomic', side_effect=fail):
            with self.assertRaises(OSError): p.prepare(self.root, self.backup, delay=0)
        self.assertFalse((self.backup/'baseline.json').exists())
        with self.assertRaises(FileNotFoundError): p.archive(self.root, self.backup)

    def test_partial_projection_write_is_retained_but_not_published(self):
        p.prepare(self.root, self.backup, delay=0)
        with patch.object(storage, 'export', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): storage.write(self.root/p.PREFIX, self.state)
        p.archive(self.root, self.backup)
        with self.assertRaisesRegex(p.PersistenceError, 'PROJECTIONS'):
            p.save(self.root, self.backup, delay=0)
        target = self.f.top/'partial-recovery'
        self.assertFalse(p.recover(self.root, self.backup, target))
        self.assertEqual((target/p.PREFIX/'adaptive_state.json').read_bytes(), (self.root/p.PREFIX/'adaptive_state.json').read_bytes())

    def test_archive_failure_cannot_publish(self):
        p.prepare(self.root, self.backup, delay=0)
        storage.write(self.root/p.PREFIX, self.state)
        original = p.write_json
        def fail(path, value):
            if path.name == 'manifest.json': raise OSError('injected')
            original(path, value)
        with patch.object(p, 'write_json', side_effect=fail):
            with self.assertRaises(OSError): p.archive(self.root, self.backup)
        with self.assertRaises(FileNotFoundError): p.save(self.root, self.backup, delay=0)
        self.assertTrue((self.backup/'adaptive/state.tar').exists())
        self.assertIn('FAILED', (self.backup/'adaptive/save_audit.jsonl').read_text())

    def test_recovery_exact_idempotent_and_refuses_overwrite(self):
        self.ready(); destination = self.f.top/'quarantine'
        self.assertTrue(p.recover(self.root, self.backup, destination))
        before = {str(x.relative_to(destination)):x.read_bytes() for x in destination.rglob('*') if x.is_file()}
        self.assertTrue(p.recover(self.root, self.backup, destination))
        self.assertEqual(before, {str(x.relative_to(destination)):x.read_bytes() for x in destination.rglob('*') if x.is_file()})
        self.f.put(destination, p.PREFIX+'adaptive_state.json', 'do not overwrite')
        with self.assertRaisesRegex(p.PersistenceError, 'NOT_IDENTICAL'): p.recover(self.root,self.backup,destination)
        self.assertEqual((destination/p.PREFIX/'adaptive_state.json').read_text(), 'do not overwrite')
        with self.assertRaisesRegex(p.PersistenceError, 'OUTSIDE'): p.recover(self.root,self.backup,self.root/'recover')

    def test_corrupt_recovery_refused_without_destination(self):
        self.ready(); path=self.backup/'adaptive/state.tar'
        path.write_bytes(path.read_bytes()+b'corruption')
        with self.assertRaisesRegex(p.PersistenceError, 'HASH_MISMATCH'):
            p.recover(self.root,self.backup,self.f.top/'quarantine')
        self.assertFalse((self.f.top/'quarantine').exists())

    def test_missing_remote_state_is_not_reinitialized(self):
        self.f.advance(p.PREFIX+'research_scan.csv', 'orphan')
        with self.assertRaises(KeyError): p.prepare(self.root,self.backup,delay=0)
        self.assertFalse((self.backup/'baseline.json').exists())

    def test_dirty_local_state_refused_before_refresh(self):
        storage.write(self.root/p.PREFIX,self.state)
        with self.assertRaisesRegex(p.PersistenceError,'DIRTY_INPUT_REFUSED'): p.prepare(self.root,self.backup,delay=0)
        self.assertEqual(storage.read(self.root/p.PREFIX),self.state)

    def test_code_or_spec_drift_blocks_save(self):
        for path in ('logic.py', 'docs/511_ADAPTIVE_RESEARCH_MODEL_LEAGUE_SPEC_v1.1_FINAL_RECONCILED.md'):
            with self.subTest(path=path):
                # Each subcase uses the same unchanged baseline: neither may push.
                if not (self.backup/'baseline.json').exists(): self.ready()
                self.f.advance(path,'changed semantics')
                with self.assertRaisesRegex(p.PersistenceError,'CODE_CHANGED'): p.save(self.root,self.backup,delay=0)

    def test_no_nonadaptive_publish_entrypoint(self):
        for group in ('research','511'):
            with self.assertRaisesRegex(p.PersistenceError,'ADAPTIVE_WRITES_ONLY'): p.archive(self.root,self.backup,group)
            with self.assertRaisesRegex(p.PersistenceError,'ADAPTIVE_WRITES_ONLY'): p.save(self.root,self.backup,group)

    def test_legacy_savers_and_adaptive_both_orders(self):
        self.ready()
        legacy_backup=self.f.top/'legacy-recovery'
        # Legacy worker is independent from the Adaptive-owned working state.
        worker=self.f.clone('legacy-worker')
        legacy.prepare(worker,legacy_backup,delay=0)
        self.f.put(worker,'strategy_pullback.csv','saved research')
        self.f.put(worker,'511_data/forward/state.json','saved beta')
        for group in ('research','511'): legacy.archive(worker,legacy_backup,group)
        legacy.save(worker,legacy_backup,'research',delay=0)
        p.save(self.root,self.backup,delay=0)
        legacy.save(worker,legacy_backup,'511',delay=0)
        self.assertEqual(self.f.read_remote('strategy_pullback.csv'),'saved research')
        self.assertEqual(self.f.read_remote('511_data/forward/state.json'),'saved beta')
        self.assertEqual(self.remote_state()['revision'],1)

    def test_adaptive_conflict_does_not_block_legacy(self):
        self.ready(); self.f.advance(p.PREFIX+'adaptive_state.json','winner')
        with self.assertRaisesRegex(p.PersistenceError,'CONFLICT'): p.save(self.root,self.backup,delay=0)
        worker=self.f.clone('legacy'); backup=self.f.top/'legacy-backup'
        legacy.prepare(worker,backup,delay=0)
        self.f.put(worker,'strategy_pullback.csv','research survives')
        legacy.archive(worker,backup,'research'); legacy.save(worker,backup,'research',delay=0)
        self.assertEqual(self.f.read_remote('strategy_pullback.csv'),'research survives')
        self.assertEqual(self.f.read_remote(p.PREFIX+'adaptive_state.json'),'winner')

    def test_cli_offline_recovery_return_code(self):
        self.ready(); destination=self.f.top/'cli-quarantine'
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).parents[2])+os.pathsep+os.environ.get('PYTHONPATH',''))
        result=subprocess.run([sys.executable,'-B','-W','error','-m','model511_adaptive.persistence','recover','--backup',str(self.backup),'--destination',str(destination)],cwd=self.root,env=env,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual((destination/p.PREFIX/'adaptive_state.json').read_bytes(),(self.root/p.PREFIX/'adaptive_state.json').read_bytes())

    def test_workflow_recovery_upload_precedes_save_and_is_required(self):
        import yaml
        text=(Path(__file__).parents[2]/'.github/workflows/model511_adaptive.yml').read_text()
        steps=yaml.safe_load(text)['jobs']['adaptive']['steps']
        archive=next(i for i,s in enumerate(steps) if s.get('id')=='archive')
        upload=next(i for i,s in enumerate(steps) if s.get('id')=='recovery')
        save=next(i for i,s in enumerate(steps) if s.get('name')=='Save Adaptive only')
        self.assertLess(archive,upload); self.assertLess(upload,save)
        self.assertIn("steps.recovery.outcome == 'success'",steps[save]['if'])
        self.assertIn('--commit "$SOURCE_COMMIT"',text)
        self.assertIn('vars.ADAPTIVE_511_ENABLED',text)
        self.assertEqual(steps[-1]['if'],'always()')

    def legacy_ready(self):
        worker, backup = self.f.clone('legacy-writer'), self.f.top/'legacy-recovery'
        legacy.prepare(worker,backup,delay=0)
        self.f.put(worker,'strategy_pullback.csv','new research')
        self.f.put(worker,'511_data/forward/state.json','new beta')
        for group in ('research','511'): legacy.archive(worker,backup,group)
        return worker,backup

    def assert_all_outputs(self):
        self.assertEqual(self.f.read_remote('strategy_pullback.csv'),'new research')
        self.assertEqual(self.f.read_remote('511_data/forward/state.json'),'new beta')
        self.assertEqual(self.remote_state()['revision'],1)

    def test_adaptive_then_both_legacy_groups(self):
        self.ready(); worker,backup=self.legacy_ready()
        p.save(self.root,self.backup,delay=0)
        for group in ('research','511'): legacy.save(worker,backup,group,delay=0)
        self.assert_all_outputs()

    def test_both_legacy_groups_then_adaptive(self):
        self.ready(); worker,backup=self.legacy_ready()
        for group in ('research','511'): legacy.save(worker,backup,group,delay=0)
        p.save(self.root,self.backup,delay=0)
        self.assert_all_outputs()

    def test_adaptive_push_during_research_push(self):
        self.ready(); worker,backup=self.legacy_ready(); original=legacy.git; calls=[]
        def race(root,*args,**kw):
            if args[0]=='push':
                calls.append(1)
                if len(calls)==1: p.save(self.root,self.backup,delay=0)
            return original(root,*args,**kw)
        with patch.object(legacy,'git',side_effect=race):
            for group in ('research','511'): legacy.save(worker,backup,group,delay=0)
        self.assertEqual(len(calls),3)
        self.assert_all_outputs()

    def test_both_legacy_pushes_during_adaptive_push(self):
        self.ready(); worker,backup=self.legacy_ready(); original=p.git; calls=[]
        def race(root,*args,**kw):
            if args[0]=='push':
                calls.append(1)
                if len(calls)==1:
                    for group in ('research','511'): legacy.save(worker,backup,group,delay=0)
            return original(root,*args,**kw)
        with patch.object(p,'git',side_effect=race): p.save(self.root,self.backup,delay=0)
        self.assertEqual(len(calls),2)
        self.assert_all_outputs()
