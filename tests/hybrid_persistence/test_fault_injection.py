import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0, str(Path.cwd()))
import hybrid_persistence as p
spec = importlib.util.spec_from_file_location('fixtures', 'tests/hybrid_persistence/test_hybrid_persistence.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

class Transport(unittest.TestCase):
    def response(self, value):
        return io.BytesIO(value)

    def redirect(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://api.github.com/fixture', 302, 'redirect', {'Location':'https://storage.example.invalid/signed'}, None)
        return opener

    def test_signed_download_never_receives_token(self):
        calls = []
        def urlopen(req, **kw):
            calls.append(req)
            return self.response(b'{}' if len(calls)==1 else b'zip')
        with patch.dict(os.environ, GITHUB_TOKEN='synthetic-test-token'), patch.object(p.urllib.request,'urlopen',side_effect=urlopen), patch.object(p.urllib.request,'build_opener',return_value=self.redirect()):
            self.assertEqual(p.remote_artifact('fixture/local','1'), ({},b'zip'))
        self.assertEqual(calls[0].get_header('Authorization'),'Bearer synthetic-test-token')
        self.assertIsInstance(calls[1],str)
        self.assertEqual(calls[1],'https://storage.example.invalid/signed')

    def test_metadata_http_and_timeout_fail_closed(self):
        for error in [TimeoutError(),urllib.error.HTTPError('https://api.github.com/fixture',503,'unavailable',{},None)]:
            with self.subTest(error=type(error).__name__), patch.dict(os.environ,GITHUB_TOKEN='synthetic-test-token'),patch.object(p.urllib.request,'urlopen',side_effect=error),self.assertRaisesRegex(p.Error,'TRANSPORT_FAILED'):
                p.remote_artifact('fixture/local','1')

    def test_download_interruption_fails_closed(self):
        with patch.dict(os.environ,GITHUB_TOKEN='synthetic-test-token'),patch.object(p.urllib.request,'urlopen',side_effect=[self.response(b'{}'),TimeoutError()]),patch.object(p.urllib.request,'build_opener',return_value=self.redirect()),self.assertRaisesRegex(p.Error,'TRANSPORT_FAILED'):
            p.remote_artifact('fixture/local','1')

    def test_capacity_limit_never_truncates_into_valid_result(self):
        with patch.dict(os.environ,GITHUB_TOKEN='synthetic-test-token'),patch.object(p,'MAX_ARTIFACT_BYTES',4),patch.object(p.urllib.request,'urlopen',side_effect=[self.response(b'{}'),self.response(b'12345')]),patch.object(p.urllib.request,'build_opener',return_value=self.redirect()),self.assertRaises(p.Error):
            p.remote_artifact('fixture/local','1')

    def test_missing_token_does_not_contact_network(self):
        with patch.dict(os.environ,GITHUB_TOKEN=''),patch.object(p.urllib.request,'urlopen') as request,self.assertRaisesRegex(p.Error,'TOKEN_REQUIRED'):
            p.remote_artifact('fixture/local','1')
        request.assert_not_called()

class AdditionalGit(unittest.TestCase):
    def setUp(self):
        self.fx = f.HybridPersistence()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_recovery_self_contains_scanner_and_completion(self):
        t=self.fx
        t.ready()
        bundle=p.load(t.backup/'recovery.json')
        self.assertIn('evidence',bundle)
        self.assertEqual(bundle['evidence']['checkpoint'],p.load(t.backup/'input-checkpoint.json'))
        self.assertEqual(bundle['evidence']['completion']['files'],bundle['files'])

    def test_recovery_evidence_tamper_refused(self):
        t=self.fx
        t.ready()
        bundle=p.load(t.backup/'recovery.json')
        for field in ('completion','checkpoint'):
            changed=json.loads(json.dumps(bundle))
            if field=='completion': changed['evidence'][field]['files']={}
            else: changed['evidence'][field]['scanner_sha256']='0'*64
            with self.subTest(field=field),self.assertRaises(p.Error): p.validate_evidence(changed)

    def test_repeated_monitor_research_updates_preserved(self):
        t=self.fx
        t.ready()
        original=p.git
        count=[]
        def git(root,*args,**kw):
            if args[0]=='push' and len(count)<3:
                count.append(len(count))
                t.advance('monitor.csv',('monitor-%d\n'%len(count)).encode())
                t.advance('research.csv',('research-%d\n'%len(count)).encode())
            return original(root,*args,**kw)
        with patch.object(p,'git',side_effect=git): p.save(t.worker,t.backup,delay=0)
        self.assertEqual(t.remote_bytes('monitor.csv'),b'monitor-3\n')
        self.assertEqual(t.remote_bytes('research.csv'),b'research-3\n')
        self.assertEqual(t.remote_bytes(p.TRADES),b'id,value\n0,initial\n1,new\n')

    def test_old_writer_after_new_save_can_overwrite_requires_drain(self):
        t=self.fx
        old=t.clone('old-running')
        stale=p.local(old)
        t.ready()
        p.save(t.worker,t.backup,delay=0)
        t.g(old,'fetch','origin','main')
        t.g(old,'reset','--hard','origin/main')
        for name,raw in stale.items(): t.put(old,name,raw)
        t.g(old,'add',*p.OWNED)
        t.g(old,'commit','-m','simulate legacy writer overwrite')
        t.g(old,'push','origin','HEAD:main')
        self.assertEqual(t.remote_bytes(p.STATE),b'{"n":0}')
        self.assertEqual(t.remote_bytes(p.TRADES),stale[p.TRADES])
        self.assertTrue((t.backup/'recovery.json').exists())

    def test_drained_old_writer_then_fresh_prepare_preserves_history(self):
        t=self.fx
        t.advance(p.STATE,b'{"n":44}')
        t.advance(p.TRADES,b'id,value\n0,initial\n44,legacy\n')
        t.ready()
        p.save(t.worker,t.backup,delay=0)
        self.assertEqual(t.remote_bytes(p.TRADES),b'id,value\n0,initial\n44,legacy\n1,new\n')
        self.assertEqual(p.decode(p.load(t.backup/'baseline.json')['inputs'])[p.STATE],b'{"n":44}')
        head=t.g(t.remote,'rev-parse','main')
        self.assertEqual(p.save(t.worker,t.backup,delay=0),head)

    def test_archive_disk_failure_no_partial_bundle_can_save(self):
        t=self.fx
        t.ready()
        (t.backup/'recovery.json').unlink()
        original=p.core.atomic
        def atomic(path,raw):
            if path.name=='recovery.json': raise OSError(28,'synthetic disk full')
            return original(path,raw)
        with patch.object(p.core,'atomic',side_effect=atomic),self.assertRaises(OSError): p.archive(t.worker,t.backup)
        with self.assertRaises(FileNotFoundError): p.save(t.worker,t.backup,delay=0)
        self.assertEqual(t.remote_bytes(p.STATE),b'{"n":0}')
        p.archive(t.worker,t.backup)
        t.confirm(t.worker,t.backup)
        p.save(t.worker,t.backup,delay=0)

    def test_input_refresh_interruption_no_baseline_certificate(self):
        t=self.fx
        t.advance(p.STATE,b'{"n":44}')
        original=p.core.atomic
        def atomic(path,raw):
            if path.name==p.TRADES: raise OSError(28,'synthetic disk full')
            return original(path,raw)
        with patch.object(p.core,'atomic',side_effect=atomic),self.assertRaises(OSError): p.prepare(t.worker,t.backup,delay=0)
        self.assertFalse((t.backup/'baseline.json').exists())
        with self.assertRaises(FileNotFoundError): p.checkpoint(t.worker,t.backup)
        self.assertEqual(t.remote_bytes(p.STATE),b'{"n":44}')

if __name__=='__main__': unittest.main(verbosity=2)
