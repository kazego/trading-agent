"""Synthetic metadata/data only. No saved experiment or market data is loaded."""
from dataclasses import replace, FrozenInstanceError
from decimal import Decimal as D
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import research.phase6d5 as p
import research.features as f
from research.data import AggregateCandle, SignalDataView, VERSION
from research.spec import ExperimentSpec, Hypothesis, HOLDOUT_START
from backtest.clock import OpeningEvent


def synthetic_views():
    views,features,histories = {},{},{}
    for symbol in p.SYMBOLS:
        candles = tuple(AggregateCandle(symbol,'4h',p.DEV_START+i*f.STEP,
            p.DEV_START+(i+1)*f.STEP-1,D(100),D(101),D(99),D(100),D(1))
            for i in range(-600,(p.VALIDATION_START-p.DEV_START)//f.STEP))
        history = candles[:600]
        hp = dict(normalized_4h_fingerprint=p.fingerprint([c.record() for c in history]))
        histories[symbol] = p.fingerprint(hp)
        hp['history_view_fingerprint'] = histories[symbol]
        package = f.HistoryPackage(symbol,history,histories[symbol],json.dumps(hp),f._ISSUED)
        meta = dict(role='DEVELOPMENT',symbol=symbol,signal_timeframe='4h',execution_resolution='15m',
            start_ms=p.DEV_START,end_ms=p.VALIDATION_START,warmup_start_ms=p.REQUEST.warmup_start_ms,
            warmup_signal_candles=99,aggregation_version=VERSION,policy_fingerprint=p.POLICY_HASH,
            source_15m_dataset_fingerprint=p.DATA_HASHES[symbol],
            normalized_aggregate_fingerprint=p.fingerprint([c.record() for c in candles[501:]]))
        meta['aggregate_fingerprint'] = p.fingerprint(meta)
        views[symbol] = SignalDataView(candles[501:],json.dumps(meta))
        with patch.dict(f.HISTORY_HASHES,{symbol:histories[symbol]}):
            features[symbol] = f.FeatureView(package,views[symbol])
    return views,features,histories


class Phase6D5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.views,cls.features,cls.histories = synthetic_views()

    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(p.HISTORY_HASHES,self.histories))
        self.stack.enter_context(patch.dict(p.VIEW_HASHES,{s:v.fingerprint for s,v in self.views.items()}))
        specs = tuple(ExperimentSpec(tf,'synthetic',(('test.py','a'*64),),tuple(p.VIEW_HASHES.items()),
            (('python','synthetic'),)) for tf in ('15m','1h','4h'))
        self.stack.enter_context(patch.object(p,'PRIOR_IDS',tuple(s.id for s in specs)))
        self.registry = p.Phase6DRegistry(self.root/'registry')
        hypothesis = self.registry.register_hypothesis(Hypothesis('Synthetic','Unit test only','No evaluation'))
        for spec in specs:
            self.registry.register_experiment(spec,hypothesis)
            step = {'15m':900000,'1h':3600000,'4h':14400000}[spec.signal_timeframe]
            request = p.SplitRequest(p.Role.DEVELOPMENT,p.DEV_START,p.VALIDATION_START,p.DEV_START-99*step)
            self.registry.record_access(spec.id,request,'READ','offline fixture')
            attempt = self.registry.begin_attempt(spec.id,request,spec.development_dataset_fingerprints)
            self.registry.finish_attempt(attempt,'COMPLETED','b'*64)  # Metadata fixture, not a run.
        self.plan = p.trial_plan()
        self.registry.register_plan(self.plan)
        self.inputs = ({s:SimpleNamespace(fingerprint=p.DATA_HASHES[s]) for s in p.SYMBOLS},
            self.views,self.features,{s:p.SymbolRules(s,'synthetic',D('.01'),D('.001'),D('.001'),D(1000),D(5)) for s in p.SYMBOLS})

    def ready(self,plan=None,**kwargs):
        return p.preflight(self.plan if plan is None else plan,registry=self.registry,
            artifact_root=self.root/'research',**kwargs)

    def modified(self,key,value,index=0):
        record=self.plan.record;record['trials'][index][key]=value
        return p.TrialPlan(p.encoded(record))

    def test_canonical_immutable_roundtrip(self):
        self.assertEqual(self.plan,p.TrialPlan(self.plan.payload))
        self.assertEqual(self.plan,p.trial_plan())
        copy=self.plan.record;copy['trials'].clear()
        self.assertEqual(len(self.plan.trials),3)
        with self.assertRaises(FrozenInstanceError):self.plan.payload=b'{}'
        with patch.object(p,'_inputs',return_value=self.inputs):
            first=self.ready();second=self.ready(p.TrialPlan(self.plan.payload))
        self.assertEqual(first.record,second.record)
        self.assertEqual(first.record['status'],'READY')

    def test_noncanonical_json_rejected(self):
        for raw in (b'{"a":1.0}',b'{"a":NaN}',b'{"a":1,"a":2}',b'{bad',b'{ "a":1}'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):p.TrialPlan(raw)

    def test_each_fingerprint_rejected_before_data_access(self):
        for key in ('rules_fingerprint','config_fingerprint','policy_fingerprint','eligibility_fingerprint',
                    'feature_implementation_fingerprint','history_manifest_fingerprint','acquisition_manifest_fingerprint'):
            with self.subTest(key=key),patch.object(p,'_inputs',side_effect=AssertionError('Data accessed')):
                with self.assertRaises(ValueError):self.ready(self.modified(key,'0'*64))
        for key in ('dataset_fingerprints','view_fingerprints','history_fingerprints','source_identity','software_identity'):
            value=self.plan.trials[0].record[key];value[next(iter(value))]='changed'
            with self.subTest(key=key),self.assertRaises(ValueError):self.ready(self.modified(key,value))

    def test_parameters_and_contract_fields_rejected(self):
        for key,value in (('strategy_parameters',{}),('protection_parameters',{}),('configuration',{}),
                ('signal_timeframe','1h'),('execution_timeframe','4h'),('symbols',['ADAUSDT']),
                ('priority',list(reversed(p.SYMBOLS))),('max_positions',2),('feature_version','changed'),
                ('feature_parameters',{}),('split',{}),('risk_basis','changed')):
            with self.subTest(key=key),self.assertRaises(ValueError):self.ready(self.modified(key,value))

    def test_missing_extra_duplicate_trial(self):
        for indices in ((0,1),(0,1,2,0),(0,1,0)):
            r=self.plan.record;r['trials']=[r['trials'][i] for i in indices]
            with self.assertRaises(ValueError):self.ready(p.TrialPlan(p.encoded(r)))

    def test_concrete_a_b_c_registration(self):
        for spec,kind,protection in zip(self.plan.trials,
                (p.RibbonStrategy,p.BreakoutStrategy,p.EMACrossover),(None,None,p.ATRProtection)):
            resolved=p.trial_mapping(spec)
            self.assertIs(resolved.strategy_type,kind);self.assertIs(resolved.protection_type,protection)

    def test_unknown_mapping_rejected(self):
        for key in ('strategy_family','strategy_version','protection_family','protection_version'):
            spec=self.modified(key,'UNKNOWN').trials[0]
            with self.assertRaises(PermissionError):p.trial_mapping(spec)

    def test_dry_context_never_evaluates_or_writes(self):
        before=self.registry.path.read_bytes()
        with ExitStack() as stack:
            stack.enter_context(patch.object(p,'_inputs',return_value=self.inputs))
            for target in ('research.phase6d5.calculate_features','research.phase6d5.execute_validated',
                    'research.phase6d5.build_scorecard','research.runner.simulate_portfolio',
                    'research.variants.RibbonStrategy.evaluate','research.variants.BreakoutStrategy.evaluate',
                    'research.strategy.EMACrossover.evaluate','research.features.FeatureView.at_open'):
                stack.enter_context(patch(target,side_effect=AssertionError('Dry construction evaluated')))
            result=self.ready()
        self.assertEqual(self.registry.path.read_bytes(),before)
        self.assertFalse((self.root/'research').exists())
        for context,kind in zip(result.contexts,(p.RibbonStrategy,p.BreakoutStrategy,p.EMACrossover)):
            self.assertTrue(all(type(s) is kind for s in context.schedule.strategies.values()))
        self.assertIsNone(result.contexts[0].protection_provider)
        self.assertIsNone(result.contexts[1].protection_provider)
        self.assertTrue(callable(result.contexts[2].protection_provider))

    def test_plan_registration_atomic_idempotent_and_append_only(self):
        before=self.registry.path.read_bytes()
        self.registry.register_plan(self.plan)
        self.assertEqual(before,self.registry.path.read_bytes())
        self.assertEqual(len([e for e in self.registry.events() if e['kind']=='PHASE6D_PLAN']),1)
        with self.assertRaises(ValueError):self.registry.register_plan(self.modified('rules_fingerprint','0'*64))

    def test_unregistered_plan_rejected(self):
        other=p.Phase6DRegistry(self.root/'empty')
        with self.assertRaises(ValueError):p.preflight(self.plan,registry=other)

    def test_derived_accounting_and_diagnostic_exclusion(self):
        before=p.accounting(self.registry,self.plan)
        with self.registry._writer() as events:
            self.registry._append(events,'DIAGNOSTIC',{'diagnostic':'Phase6D4','fingerprint':'d'*64})
        self.assertEqual(before,p.accounting(self.registry,self.plan))
        self.assertEqual(before['future_total_strategy_configurations'],6)
        self.assertEqual(before['phase6d_attempts'],0)

    def test_historical_configuration_add_remove_fail_reconciliation(self):
        events=self.registry.events()
        for changed in ([e for e in events if e['kind']!='EXPERIMENT'],
                events+[next(e for e in events if e['kind']=='EXPERIMENT')]):
            with patch.object(self.registry,'events',return_value=changed),self.assertRaises(ValueError):
                p.accounting(self.registry,self.plan)

    def test_corrupt_ledger_fails_closed(self):
        raw=self.registry.path.read_bytes();self.registry.path.write_bytes(raw[:-1])
        with self.assertRaises(ValueError):self.ready()

    def test_retry_does_not_create_configuration_or_replace_completion(self):
        spec=self.plan.trials[0]
        self.registry.record_access(spec.id,p.REQUEST,'READ','synthetic')
        attempt=self.registry.begin_attempt(spec.id,p.REQUEST,p.VIEW_HASHES.items())
        self.registry.finish_attempt(attempt,'FAILED',error='synthetic infrastructure')
        retry=self.registry.begin_attempt(spec.id,p.REQUEST,p.VIEW_HASHES.items())
        self.assertNotEqual(attempt,retry)
        self.assertEqual(p.accounting(self.registry,self.plan)['future_total_strategy_configurations'],6)
        self.registry.finish_attempt(retry,'COMPLETED','c'*64)
        with self.assertRaises(ValueError):self.registry.begin_attempt(spec.id,p.REQUEST,p.VIEW_HASHES.items())
        with self.assertRaises(ValueError):self.registry.finish_attempt(retry,'FAILED',error='replace')

    def test_validation_holdout_and_history_denied(self):
        spec=self.plan.trials[0]
        for request in (p.SplitRequest(p.Role.VALIDATION,p.VALIDATION_START,HOLDOUT_START,p.VALIDATION_START-99*f.STEP),
                p.SplitRequest(p.Role.HOLDOUT,HOLDOUT_START,HOLDOUT_START+f.STEP,HOLDOUT_START-99*f.STEP),
                replace(p.REQUEST,warmup_start_ms=p.DEV_START-600*f.STEP)):
            with self.assertRaises(PermissionError):self.registry.record_access(spec.id,request,'READ','test')
        with self.assertRaises(PermissionError):self.registry.promote_validation(spec.id,'test','test')
        with self.assertRaises(PermissionError):self.registry.authorize_holdout(spec.id,HOLDOUT_START+f.STEP,'test','test')

    def test_empty_malformed_missing_and_mutated_manifests(self):
        path=self.root/'manifest.json'
        for body in ('{}','{bad','[]','{"fingerprint":"wrong"}','{"a":1,"a":1}'):
            path.write_text(body)
            with self.subTest(body=body),self.assertRaises(ValueError):p._manifest(path,'a'*64,'fingerprint')
        valid={'field':'expected'};digest=p.fingerprint(valid)
        path.write_bytes(p.encoded(dict(valid,fingerprint=digest)))
        self.assertEqual(p._manifest(path,digest,'fingerprint')['field'],'expected')
        path.write_bytes(p.encoded(dict(field='mutated',fingerprint=digest)))
        with self.assertRaises(ValueError):p._manifest(path,digest,'fingerprint')

    def test_empty_manifest_fails_actual_preflight_input_path(self):
        (self.root/'acquisition-manifest.json').write_text('{}')
        with self.assertRaises(ValueError):self.ready(historical_root=self.root)

    def test_completed_and_partial_artifact_collision(self):
        for i,name in enumerate(('first-result.json','approved-inputs.json')):
            output=p.artifact_destination(self.root/'research',self.plan.trials[i].id)
            output.mkdir(parents=True);(output/name).write_text('{}')
        with patch.object(p,'_inputs',side_effect=AssertionError('Read before collision check')):
            with self.assertRaises(FileExistsError):self.ready()

    def test_mismatched_artifact_identity(self):
        (self.root/'research'/p.TRIAL_IDS[0]).mkdir(parents=True)
        with self.assertRaises(ValueError):self.ready()
        with self.assertRaises(ValueError):p.artifact_destination(self.root/'research','../bad')

    def test_shared_locator_and_runner_binding_no_execution(self):
        with patch.object(p,'_inputs',return_value=self.inputs):contexts=self.ready().contexts
        for context in contexts:
            self.assertEqual(context.destination(self.root/'research'),p.artifact_destination(self.root/'research',context.spec.id))
            self.registry.record_access(context.spec.id,p.REQUEST,'READ','synthetic')
            attempt=self.registry.begin_attempt(context.spec.id,p.REQUEST,p.VIEW_HASHES.items())
            with patch.object(p,'build_view',side_effect=lambda data,symbol,tf,req:self.views[symbol]):
                bound=context.bind_runner(self.registry,attempt,self.root/'research')
            self.assertIs(bound.func,p.execute_validated)
            self.assertIs(bound.keywords['schedule'],context.schedule)
            self.assertEqual(bound.keywords['protection_provider'],context.protection_provider)
        self.assertFalse((self.root/'research').exists())

    def test_c_requires_exact_saved_entry_snapshot_and_a_b_deny_atr(self):
        with patch.object(p,'_inputs',return_value=self.inputs):contexts=self.ready().contexts
        event=OpeningEvent('BTCUSDT','15m',p.DEV_START,p.DEV_START-1,D(100),(),p.DATA_HASHES['BTCUSDT'])
        decision=SimpleNamespace(signal='LONG');snapshot=object()
        for context in contexts[:2]:
            with self.assertRaises(PermissionError):context.schedule.protection_for_entry(decision,event)
        schedule=contexts[2].schedule
        with self.assertRaises(ValueError):schedule.protection_for_entry(decision,event)
        schedule._entries[event.symbol]=(decision,event,snapshot)
        # Verify exact object handoff, without calculating ATR, signals or trades.
        with patch.object(p.ATRProtection,'__init__',return_value=None) as constructor:
            result=schedule.protection_for_entry(decision,event)
            self.assertIs(type(result),p.ATRProtection)
            self.assertIs(constructor.call_args.args[1],snapshot)
            self.assertIs(constructor.call_args.args[0],self.features['BTCUSDT'])
        with self.assertRaises(ValueError):schedule.protection_for_entry(decision,replace(event,open_time_ms=p.DEV_START+f.STEP))

    def test_policy_identities_preserved(self):
        self.assertEqual(p.fingerprint(p.policy()),p.POLICY_HASH)
        self.assertEqual(p.fingerprint(p.policy()['eligibility']),p.ELIGIBILITY_HASH)

    def test_actual_rules_identity_rejected_before_candle_loading(self):
        bad=self.root/'rules.json';bad.write_text('{}')
        with patch.object(p,'_manifest'),patch.object(p,'load_local_development',side_effect=AssertionError('Read')):
            with self.assertRaises(ValueError):p._inputs(self.root,self.root,bad)

    def test_context_rules_or_data_substitution_rejected(self):
        with patch.object(p,'_inputs',return_value=self.inputs):context=self.ready().contexts[0]
        for changed in (replace(context,rules={}),replace(context,datasets={}),replace(context,_issuer=None)):
            with self.assertRaises((ValueError,PermissionError)):changed.bind_runner(self.registry,'unknown',self.root/'research')

    def test_variant_signal_envelope_preserves_identity_without_evaluation(self):
        from research.variants import VariantDecision
        for implementation in (p.RibbonStrategy,p.BreakoutStrategy):
            signal=VariantDecision(symbol='BTCUSDT',signal_timeframe='4h',open_time_ms=p.DEV_START,signal='NONE',
                strategy_family=implementation.family,strategy_version=implementation.version,
                parameters=implementation.parameters,visible_fingerprint='a'*64,completed_open_times_ms=(p.DEV_START-f.STEP,),
                reason='SYNTHETIC_ENVELOPE',source_sha256='b'*64,python_version='test',pandas_version='test',
                numpy_version='test',decision_id='c'*64,feature_fingerprint='d'*64,trigger='NONE')
            event=OpeningEvent('BTCUSDT','15m',p.DEV_START,p.DEV_START-1,D(100),(),p.DATA_HASHES['BTCUSDT'])
            one=p._variant_execution(signal,event);two=p._variant_execution(signal,event)
            self.assertEqual(one,two);self.assertEqual(one.signal_decision_id,signal.decision_id)
            self.assertEqual(one.feature_fingerprint,signal.feature_fingerprint)
            self.assertEqual(one.ema_spans,implementation.parameters.trigger_ema_spans)


if __name__=='__main__':unittest.main()
