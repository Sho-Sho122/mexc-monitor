"""Certification integrity; no strategy or performance tuning."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from test_adaptive import prepared_final, evidence, event, T, state, challenger
from model511_adaptive import forward, final_confirmation as final
from model511_adaptive.config import iso, digest
from model511_adaptive.storage import write, read
from model511_adaptive.__main__ import main
from model511_adaptive import gate
from model511_adaptive.completeness import migrate, ingest, certification, population
from model511_adaptive.semantics import register_feature, effective, membership, model_blocks
from model511_adaptive.config import base_model
from model511_adaptive.storage import validate, export
import random
import subprocess
import sys


class ReproducedP1(unittest.TestCase):
    def setup_population(self):
        s,c,f,at = prepared_final()
        evidence(s,c,14,12,at)
        cutoff=at+timedelta(days=14)
        e=event(cutoff-timedelta(minutes=1),'missing-unresolved')
        s['last_update_jst']=iso(cutoff+timedelta(minutes=5))
        source={'evaluation_mode':'FORWARD','events':list(s['source_history'].values())+[e],
                'last_processed_bucket':iso(cutoff-timedelta(minutes=5))}
        return s,c,f,cutoff,e,source

    def test_legacy_body_missing_never_disappears(self):
        s,c,f,cutoff,e,source=self.setup_population()
        s['seen_events'][e['event_id']]={'hash':digest(e),'first_seen_jst':iso(cutoff+timedelta(minutes=5))}
        with tempfile.TemporaryDirectory() as d:
            write(d,s); s=read(d)
        forward.update(s,source,{},cutoff+timedelta(minutes=10))
        f=final.evaluate(s,f['final_id'],cutoff+timedelta(minutes=10))
        self.assertEqual(f['final_decision_count'],0)
        self.assertIn(e['event_id'], f['evaluations'][-1]['pending_reference_event_ids'])

    def test_builtin_collision_rejected_and_population_immutable(self):
        s,c,f,cutoff,e,source=self.setup_population()
        forward.update(s,source,{},cutoff+timedelta(minutes=10))
        before=forward.cohort(s,c['challenger_id'],cutoff,f['final_start_jst'],purpose='final')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); write(root/'state/forward',s)
            req=root/'request.json'; req.write_text(json.dumps({'name':'has_pullback','definition':'collision'}))
            with patch('model511_adaptive.__main__.snapshot',return_value={'issues':[],'snapshot_id':'clean'}), patch('model511_adaptive.__main__.now',return_value=cutoff+timedelta(minutes=15)):
                with self.assertRaises(ValueError):
                    main(['register-feature','--state-dir',str(root/'state'),'--source-dir',str(root/'source'),
                          '--request',str(req),'--commit','test'])
            loaded=read(root/'state/forward')
        self.assertEqual(before,forward.cohort(loaded,c['challenger_id'],cutoff,f['final_start_jst'],purpose='final'))


class CompletenessProperties(unittest.TestCase):
    def sample(self):
        s=state(); c=challenger(s)
        events=[event(T+timedelta(minutes=5+5*i),str(i),symbol='Z' if i%2 else 'A') for i in range(4)]
        at=T+timedelta(hours=1)
        beta={'evaluation_mode':'FORWARD','events':events,'forward_start_jst':iso(T+timedelta(minutes=5)),
              'last_processed_bucket':iso(at-timedelta(minutes=5)), 'last_cutoff_jst':iso(at)}
        return s,c,events,beta,at

    def signature(self,s,c,at):
        rows,blocks=certification(s,c,c['forward_start_jst'],at,'final')
        return {'model':c['effective_model_spec'],'hash':c['model_spec_hash'],
                'population':rows,'blocks':blocks,
                'memberships':[(r['event_id'],membership(c,r,at)) for r in s['source_history'].values()],
                'pending':sum(not r.get('exit_result') for r in rows),
                'status':'BLOCKED' if blocks else 'READY'}

    def test_source_order_restart_schema_and_materialization_matrix(self):
        s,c,events,beta,at=self.sample()
        expected=None
        orders=[events,list(reversed(events)),sorted(events,key=lambda e:e['symbol'])]
        for seed in (0,1,19,511,9347):
            shuffled=copy.deepcopy(events); random.Random(seed).shuffle(shuffled); orders.append(shuffled)
        for order in orders:
            for legacy in (False,True):
                for missing in ([],['0'],['0','1','2','3']):
                    with self.subTest(order=[e['event_id'] for e in order],legacy=legacy,missing=missing):
                        a=copy.deepcopy(s)
                        for e in events:
                            a['seen_events'][e['event_id']]={'hash':digest(e),'first_seen_jst':iso(at),**({} if e['event_id'] in missing else {'event':copy.deepcopy(e)})}
                        if legacy: a['schema_version']=1
                        a=migrate(a)
                        with tempfile.TemporaryDirectory() as d:
                            write(d,a); a=read(d) # restart between structural migration and source recovery
                            forward.update(a,{**beta,'events':order},{},at)
                            first=copy.deepcopy(a)
                            forward.update(a,{**beta,'events':order},{},at)
                            self.assertEqual(a,first)
                            for _ in range(3): write(d,a); a=read(d)
                        sig=self.signature(a,a['challengers'][0],at)
                        if expected is None: expected=sig
                        self.assertEqual(sig,expected)

    def test_unknown_or_future_schema_rejected(self):
        s,_,_,_,_=self.sample()
        for version in (0,3,99,None):
            with self.subTest(version=version), self.assertRaises(ValueError): migrate({**s,'schema_version':version})
        self.assertEqual(migrate(s),s)

    def test_legacy_migration_deterministic_audit_and_no_downgrade(self):
        s,_,events,beta,at=self.sample(); s['schema_version']=1
        e=events[0]; s['seen_events'][e['event_id']]={'hash':digest(e),'first_seen_jst':iso(at)}
        a=migrate(s); self.assertEqual(a,migrate(copy.deepcopy(s))); self.assertEqual(a,migrate(a))
        self.assertEqual(a['schema_version'],2); self.assertEqual(a['audit'][-1]['action'],'SCHEMA_MIGRATION')
        ingest(a,beta,at); self.assertEqual(a['seen_events']['0']['event'],e)
        first=copy.deepcopy(a); ingest(a,beta,at); self.assertEqual(first,a)
        with self.assertRaises(ValueError): validate({**a,'schema_version':1})

    def test_missing_source_cannot_qualify_any_path(self):
        s,c,events,beta,at=self.sample()
        s['seen_events']['unknown']={'hash':'unrecoverable','first_seen_jst':iso(at)}
        forward.update(s,beta,{},at)
        for purpose in ('gate','practice','final'):
            rows,blocks=certification(s,c,c['forward_start_jst'],at,purpose)
            self.assertIn('SOURCE_HISTORY_INCOMPLETE',blocks)
            self.assertIn('SOURCE_BODY_MISSING',blocks)
            self.assertEqual(len(rows),4)

    def test_legacy_semantics_are_not_rebuilt_from_registry(self):
        s,c,_,beta,at=self.sample(); s['schema_version']=1
        del c['effective_model_spec']; c['model_spec_hash']=digest(c['model_spec_json'])
        a=migrate(s)
        forward.update(a,beta,{},at)
        self.assertIn('FROZEN_SEMANTICS_UNRESOLVED',certification(a,a['challengers'][0],c['forward_start_jst'],at,'final')[1])
        self.assertNotIn('effective_model_spec',a['challengers'][0])

    def test_changed_source_cannot_repair_legacy_hash(self):
        s,c,events,beta,at=self.sample()
        s['seen_events']['0']={'hash':digest(events[0]),'first_seen_jst':iso(at)}
        changed=copy.deepcopy(beta); changed['events'][0]['crowding_state']='MATCH'
        forward.update(s,changed,{},at)
        self.assertIn('0',s['population_unresolved_ids'])
        self.assertNotIn('event',s['seen_events']['0'])

    def test_future_source_and_outcome_never_improve_past(self):
        s,c,events,beta,at=self.sample()
        forward.update(s,beta,{},at)
        rows,_=population(s,c,c['forward_start_jst'],at,'final')
        e=event(at+timedelta(minutes=5),'future')
        later=at+timedelta(hours=1)
        forward.update(s,{**beta,'events':events+[e],'last_processed_bucket':iso(later-timedelta(minutes=5))},{},later)
        r=s['evidence'][0]; r.update(exit_result='TP',gross_R=1,net_R=.9,reference_resolved_at_jst=iso(later),execution_resolved_at_jst=iso(later))
        after,_=population(s,c,c['forward_start_jst'],at,'final')
        self.assertEqual([r['event_id'] for r in rows],[r['event_id'] for r in after])
        self.assertTrue(all(r['net_R'] is None and not r['exit_result'] for r in after))

    def test_materialization_time_is_not_population_filter(self):
        s,c,_,beta,at=self.sample(); forward.update(s,beta,{},at)
        before,_=population(s,c,c['forward_start_jst'],at,'final')
        for r in s['memberships']+s['evidence']: r['decision_at_jst']=iso(at+timedelta(days=5))
        after,_=population(s,c,c['forward_start_jst'],at,'final')
        self.assertEqual([r['event_id'] for r in before],[r['event_id'] for r in after])

    def test_failed_update_is_atomic_and_restart_retries(self):
        s,c,events,beta,at=self.sample(); before=copy.deepcopy(s)
        broken=copy.deepcopy(beta); broken['events'][-1]['evaluation_mode']='BACKFILL'
        with self.assertRaises(ValueError): forward.update(s,broken,{},at)
        self.assertEqual(s,before)
        forward.update(s,beta,{},at)
        with tempfile.TemporaryDirectory() as d:
            write(d,s); a=read(d); forward.update(a,beta,{},at); self.assertEqual(s,a)

    def test_evidence_cannot_supply_source_completeness(self):
        s,c,_,beta,at=self.sample(); forward.update(s,beta,{},at)
        s['source_history'].clear(); s['seen_events'].clear()
        rows,blocks=certification(s,c,c['forward_start_jst'],at,'final')
        self.assertEqual(rows,[]); self.assertIn('SOURCE_BODY_MISSING',blocks)

    def test_evidence_only_restart_does_not_duplicate(self):
        s,c,events,beta,at=self.sample(); forward.update(s,beta,{},at)
        original=copy.deepcopy(s['evidence'])
        s['memberships']=[]; s['seen_events']={}; s['last_update_jst']=None
        forward.update(s,beta,{},at)
        self.assertEqual(s['evidence'],original)
        self.assertEqual(len(s['memberships']),4)
        validate(s)

    def test_id_without_hash_cannot_be_assumed_materialized(self):
        for missing in (None,{},'id-only'):
            s,c,events,beta,at=self.sample(); s['seen_events']['0']=missing
            forward.update(s,beta,{},at)
            self.assertIn('0',s['population_unresolved_ids'])
            self.assertTrue(certification(s,c,c['forward_start_jst'],at,'final')[1])

    def test_net_provenance_and_component_guard(self):
        s,c,f,at=prepared_final(); evidence(s,c,14,12,at)
        cutoff=at+timedelta(days=14)
        self.assertEqual(certification(s,c,at,cutoff,'final')[1],[])
        target=next(i for i,r in enumerate(s['evidence']) if r['entry_time_jst']>=iso(at))
        for key,value in [('fee_R',None),('slippage_R',None),('spread_R',None),('net_R',None),('cost_settings',{}),('entry_execution',{}),('exit_quote_timestamp',None),('net_R',999)]:
            with self.subTest(key=key,value=value):
                a=copy.deepcopy(s); a['evidence'][target][key]=value
                self.assertTrue(certification(a,c,at,cutoff,'final')[1])

    def test_report_and_registry_mutations_preserve_population(self):
        from model511_adaptive.reporting import report
        s,c,events,beta,at=self.sample(); forward.update(s,beta,{},at)
        expected=self.signature(s,c,at)
        register_feature(s,{'name':'unused','definition':'unused definition'},T)
        s['feature_catalog']['has_pullback']={'definition':'hostile current registry','feature_available_from_jst':iso(at)}
        report(s,at); report(s,at+timedelta(days=1))
        self.assertEqual(self.signature(s,c,at),expected)
        with tempfile.TemporaryDirectory() as d:
            write(d,s); loaded=read(d)
        self.assertEqual(self.signature(loaded,loaded['challengers'][0],at),expected)

    def test_standalone_final_refreshes_source_without_update(self):
        from model511_adaptive.runner import run
        s,c,f,at=prepared_final(); evidence(s,c,14,12,at)
        cutoff=at+timedelta(days=14); e=event(cutoff-timedelta(minutes=1),'standalone-late')
        source={'issues':[],'snapshot_id':'complete','manifest':[],'source_commit':'test','source_schema_version':'1',
                'beta':{'evaluation_mode':'FORWARD','events':list(s['source_history'].values())+[e],
                        'last_processed_bucket':iso(cutoff-timedelta(minutes=5))}}
        result=run(s,source,cutoff+timedelta(minutes=10),scans=False,gates=False,finals=True)
        final_result=result['final_confirmations'][0]
        self.assertEqual(final_result['final_decision_count'],0)
        self.assertIn(e['event_id'],final_result['evaluations'][-1]['pending_reference_event_ids'])

    def test_feature_cli_repeat_is_full_state_noop(self):
        s=state(); req={'name':'unused','definition':'immutable'}
        register_feature(s,req,T)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); write(root/'state/forward',s)
            request=root/'req.json'; request.write_text(json.dumps(req))
            with patch('model511_adaptive.__main__.now',return_value=T+timedelta(days=1)):
                main(['register-feature','--state-dir',str(root/'state'),'--source-dir',str(root/'source'),'--commit','test','--request',str(request)])
            self.assertEqual(read(root/'state/forward'),s)

    def test_cli_update_matches_direct_function_and_exit_codes(self):
        s,c,events,beta,at=self.sample()
        direct=copy.deepcopy(s); forward.update(direct,beta,{},at)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); write(root/'state/forward',s)
            with patch('model511_adaptive.__main__.snapshot',return_value={'issues':[],'snapshot_id':'clean','beta':beta}), patch('model511_adaptive.__main__.now',return_value=at):
                main(['update','--state-dir',str(root/'state'),'--source-dir',str(root/'source'),'--commit','test'])
            cli=read(root/'state/forward')
            self.assertEqual(self.signature(direct,c,at),self.signature(cli,cli['challengers'][0],at))
            repo=Path(__file__).resolve().parents[2]
            for command in ('report','validate-state'):
                completed=subprocess.run([sys.executable,'-B','-W','error','-m','model511_adaptive',command,'--state-dir',str(root/'state')],cwd=repo,text=True,capture_output=True)
                self.assertEqual(completed.returncode,0,completed.stderr)
                json.loads(completed.stdout)
            failed=subprocess.run([sys.executable,'-B','-m','model511_adaptive','update','--as-of',iso(at)],cwd=repo,text=True,capture_output=True)
            self.assertEqual(failed.returncode,2)


class FrozenSemanticProperties(unittest.TestCase):
    def test_unused_registration_and_idempotent_reregistration(self):
        s=state(); c=challenger(s); before=copy.deepcopy(c)
        req={'name':'custom_v1','definition':'exact input field','version':'1'}
        register_feature(s,req,T); registered=copy.deepcopy(s)
        register_feature(s,req,T+timedelta(days=1)); self.assertEqual(s,registered)
        self.assertEqual(c,before); self.assertEqual(model_blocks(c),[])
        with self.assertRaises(ValueError): register_feature(s,{**req,'definition':'different'},T)
        register_feature(s,{**req,'name':'custom_v2','version':'2'},T)
        self.assertEqual(c,before)

    def test_frozen_custom_feature_survives_registry_clear_order_and_reload(self):
        s=state(); register_feature(s,{'name':'custom','definition':'number','version':'1'},T-timedelta(days=1))
        model=base_model(); model['entry'].append({'field':'custom','op':'ge','value':1})
        c=challenger(s,model); e=event(T+timedelta(minutes=5)); e['custom']=2
        before=membership(c,e)
        s['feature_catalog'].clear()
        self.assertEqual(membership(c,e),before); self.assertEqual(model_blocks(c),[])
        with tempfile.TemporaryDirectory() as d:
            write(d,s); loaded=read(d)
        self.assertEqual(membership(loaded['challengers'][0],e),before)

    def test_builtin_collision_direct(self):
        s=state()
        for name in ('has_pullback','crowding_state','direction','asset_class'):
            with self.subTest(name=name),self.assertRaises(ValueError): register_feature(s,{'name':name,'definition':'override'},T)

    def test_semantic_hash_audit(self):
        s=state(); base=base_model(); original=effective(base,s['feature_catalog'])
        variants=[]
        for field,value in [('sl_pct',.75),('rr',2),('max_hours',24)]:
            m=copy.deepcopy(base); m['exit'][field]=value; variants.append(m)
        for field,value in [('has_pullback',False),('crowding_state','MATCH')]:
            m=copy.deepcopy(base); next(c for c in m['entry'] if c['field']==field)['value']=value; variants.append(m)
        for field,value in [('direction','SHORT'),('symbol','X_USDT'),('asset_class','UNKNOWN')]:
            m=copy.deepcopy(base); m['entry'].append({'field':field,'op':'eq','value':value}); variants.append(m)
        for m in variants: self.assertNotEqual(digest(effective(m,{})),digest(original))
        permuted=dict(reversed(list(base.items()))); permuted['entry']=list(reversed(base['entry']))
        self.assertEqual(effective(permuted,{}),original)
        for req in ({'name':'x1','definition':'one'},{'name':'x2','definition':'two'}): register_feature(s,req,T)
        self.assertEqual(effective(base,s['feature_catalog']),effective(base,dict(reversed(list(s['feature_catalog'].items())))))
        m=copy.deepcopy(base); m['feature_schema_version']='42'
        self.assertEqual(effective(m,s['feature_catalog']),original)
        for key in ('engines','population_policy','execution_settings_policy','features'):
            changed=copy.deepcopy(original); changed[key]={'changed':True}
            self.assertNotEqual(digest(original),digest(changed))

    def test_feature_definition_version_and_availability_are_identity(self):
        from model511_adaptive.semantics import feature
        a=feature('custom','meaning','1',iso(T))
        variants=[feature('custom','new meaning','1',iso(T)),feature('custom','meaning','2',iso(T)),feature('custom','meaning','1',iso(T+timedelta(days=1)))]
        m=base_model(); m['entry'].append({'field':'custom','op':'ge','value':1})
        baseline=digest(effective(m,{'custom':a}))
        for f in variants: self.assertNotEqual(digest(effective(m,{'custom':f})),baseline)

    def test_semantic_mutation_blocked_runtime_metadata_ignored(self):
        s=state(); c=challenger(s); initial=copy.deepcopy(c)
        c.update(display_name='label',report_timestamp=iso(T),storage_path='diagnostic-only')
        self.assertEqual(c['model_spec_hash'],initial['model_spec_hash']); self.assertEqual(model_blocks(c),[])
        c['model_spec_json']['exit']['rr']=2
        self.assertIn('MODEL_HASH_MISMATCH',model_blocks(c))

    def test_engine_semantic_change_requires_new_hash(self):
        s=state(); c=challenger(s)
        changed={**c['effective_model_spec']['engines'],'regime_rule':'changed'}
        with patch('model511_adaptive.semantics.engine_identity',return_value=changed):
            self.assertIn('SEMANTIC_ENGINE_MISMATCH',model_blocks(c))
            self.assertNotEqual(digest(effective(base_model(),{})),c['model_spec_hash'])

    def test_numeric_canonical_forms_and_predicate_order(self):
        a=base_model(); a['entry'].append({'field':'strategy_count','op':'in','value':[1,2,0]})
        b=copy.deepcopy(a); b['entry'][-1]['value']=[-0.0,2.0,1.0,1.0]; b['entry'].reverse()
        self.assertEqual(effective(a,{}),effective(b,{}))

    def test_legacy_sealed_final_cannot_be_read_as_current_certification(self):
        s,c,f,at=prepared_final(); evidence(s,c,14,12,at)
        final.evaluate(s,f['final_id'],at+timedelta(days=14))
        self.assertEqual(f['final_decision'],'PASS')
        del c['effective_model_spec']
        with self.assertRaises(ValueError): final.evaluate(s,f['final_id'],at+timedelta(days=15))
        from model511_adaptive.reporting import report
        reported=report(s,at+timedelta(days=15))['finals'][0]
        self.assertIsNone(reported['final_decision'])
        self.assertEqual(reported['historical_final_decision'],'PASS')
        self.assertEqual(reported['status'],'UNRESOLVED')
