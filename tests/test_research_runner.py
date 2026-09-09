"""Offline synthetic-only runner, analytics, and artifact contracts."""
from dataclasses import replace, asdict
from decimal import Decimal as D, localcontext
from itertools import islice
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from backtest.clock import HistoricalClock
from backtest.data import _hash
from backtest.metrics import analyze
from backtest.portfolio import simulate_portfolio
from research.data import build_view
from research.registry import Registry
from research.runner import (approved_configuration, source_identity, software_identity,
    planned_batch, validate_inputs, execute, human_report, write_comparison, ledger_summary)
from research.scorecard import outcome_metrics, eligibility, build_scorecard, compare_batch
from research.spec import (SYMBOLS, DEV_START, VALIDATION_START, HOLDOUT_START, STEP_MS,
    ExperimentSpec, Hypothesis, SplitRequest, Role, canonical, fingerprint, encoded)
from research.strategy import EMACrossover, VERSION
import test_research_data as dh
import test_backtest_portfolio as ph


def register(directory,spec,request):
    registry=Registry(directory)
    h=registry.register_hypothesis(Hypothesis('Synthetic test','No market research','Unit test'))
    registry.register_experiment(spec,h)
    registry.record_access(spec.id,request,'READ','offline-test')
    attempt=registry.begin_attempt(spec.id,request,spec.development_dataset_fingerprints)
    return registry,attempt


class ScorecardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=ph.datasets()
        cls.result=simulate_portfolio(cls.data,ph.rules(),approved_configuration())

    def card(self): return build_scorecard(self.result,self.data,{'experiment_fingerprint':'a'*64})

    def test_reconciliation_and_drawdown_reuse(self):
        c=self.card();a=analyze(self.result,self.data)
        self.assertEqual(c['risk']['mtm'],canonical(a['risk']['mtm']))
        self.assertEqual(c['risk']['realized'],canonical(a['risk']['realized']))
        self.assertEqual(D(c['performance']['gross_pnl'])-D(c['costs']['modeled_fees']),D(c['performance']['net_pnl']))

    def test_pf_and_expectancy_edges(self):
        self.assertIsNone(outcome_metrics([])['net_profit_factor']['value'])
        t=self.result.trades[0]
        ts=[replace(t,gross_pnl=D(6),net_pnl=D(5)),replace(t,gross_pnl=D(-1),net_pnl=D(-2))]
        m=outcome_metrics(ts)
        self.assertEqual(m['net_profit_factor']['value'],D('2.5'))
        self.assertEqual(m['profit_factor']['value'],D(6))
        self.assertEqual(m['expectancy_per_trade'],D('1.5'))
        self.assertEqual(outcome_metrics(ts[:1])['net_profit_factor']['status'],'NO_NET_LOSSES')

    def test_calendar_complete_and_attribution_reconciles(self):
        c=self.card()
        self.assertEqual(len(c['temporal']['months']),12)
        self.assertEqual(len(c['temporal']['quarters']),4)
        for groups in (c['temporal']['quarters'],c['temporal']['months'],c['attribution']['symbol_direction'],c['attribution']['direction']):
            self.assertEqual(sum(D(v['net_pnl']) for v in groups.values()),D(c['performance']['net_pnl']))

    def test_eligibility_target_not_hard_rejection(self):
        m=dict(expectancy_per_trade=D(1),net_profit_factor=dict(status='DEFINED',value=D(2)),total_trades=99)
        q={str(i):dict(total_trades=2,net_pnl=D(1)) for i in range(4)}
        self.assertEqual(eligibility(m,q,D(24),integrity=True,insolvent=False)['status'],'WEAK_EVIDENCE')
        m['total_trades']=100
        self.assertEqual(eligibility(m,q,D(24),integrity=True,insolvent=False)['status'],'ELIGIBLE')
        for dd,ok,ins in [(D(25),True,False),(D(1),False,False),(D(1),True,True)]:
            self.assertEqual(eligibility(m,q,dd,integrity=ok,insolvent=ins)['status'],'INELIGIBLE')

    def test_inactive_and_losing_quarters_block(self):
        m=dict(expectancy_per_trade=D(1),net_profit_factor=dict(status='DEFINED',value=D(1)),total_trades=100)
        q={str(i):dict(total_trades=0,net_pnl=D(0)) for i in range(4)}
        reasons=eligibility(m,q,D(0),integrity=True,insolvent=False)['reasons']
        self.assertIn('NET_PF_NOT_ABOVE_ONE',reasons)
        self.assertIn('INACTIVE_QUARTER',reasons)
        self.assertIn('FEWER_THAN_THREE_POSITIVE_QUARTERS',reasons)

    def test_canonical_deterministic_and_read_only(self):
        before=self.result.fingerprint
        c=self.card()
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(c,self.card())
        self.assertEqual(before,self.result.fingerprint)
        self.assertEqual(c['scorecard_fingerprint'],fingerprint({k:v for k,v in c.items() if k!='scorecard_fingerprint'}))
        with self.assertRaises(ValueError):encoded({'x':1.0})
        with self.assertRaises(ValueError):encoded({'x':D('NaN')})

    def test_report_not_validation_or_profitability_claim(self):
        text=human_report(self.card())
        for phrase in ('DEVELOPMENT ONLY','not validation or holdout','funding and liquidation excluded','No claim of future profitability','static current exchange rules'):
            self.assertIn(phrase,text)

    def test_comparison_pareto_and_no_promotion(self):
        plans=planned_batch({tf:{s:'a'*64 for s in SYMBOLS} for tf in ('15m','1h','4h')},source_hashes=(('test','b'*64),),software_versions=(('test','1'),))
        cards=[]
        for i,s in enumerate(plans):
            c=self.card();c['identity']['experiment_fingerprint']=s.id
            c['performance']['return_pct']=str(i)
            c['performance']['expectancy_per_trade']=str(i)
            c['scorecard_fingerprint']=fingerprint({k:v for k,v in c.items() if k!='scorecard_fingerprint'})
            cards.append(c)
        comparison=compare_batch(plans,cards)
        self.assertIn(['4h','15m'],comparison['dominates'])
        self.assertFalse(comparison['automatic_promotion'])
        self.assertEqual(comparison,compare_batch(plans,list(reversed(cards))))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'comparison';write_comparison(comparison,p)
            with self.assertRaises(FileExistsError):write_comparison(comparison,p)
        with self.assertRaises(ValueError):compare_batch(plans,cards[:-1])


class RunnerGatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Entirely generated flat synthetic inputs, never saved market files.
        count=(VALIDATION_START-DEV_START)//STEP_MS
        def component(s,i):
            c=dh.candle(DEV_START+i*STEP_MS,s)
            if i==-2: return replace(c,close=D(90),low=D(89))
            if i==-1: return replace(c,close=D(110),high=D(111))
            if i==1: return replace(c,low=D(97))
            return c
        cls.data={s:dh.dataset([component(s,i) for i in range(-99,count)],
            warmup=99,symbols=[s]) for s in SYMBOLS}
        cls.request=dh.request()
        cls.views={s:build_view(cls.data[s],s,'15m',cls.request) for s in SYMBOLS}
        cls.spec=ExperimentSpec('15m',VERSION,source_identity(),tuple((s,cls.views[s].fingerprint) for s in SYMBOLS),software_identity())
        cls.snapshot=dict(normalized_rules={s:canonical(asdict(r)) for s,r in ph.rules().items()})
        cls.rule_hash=_hash(cls.snapshot)
        cls.snapshot['rules_fingerprint']=cls.rule_hash

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry,self.attempt=register(self.temp.name,self.spec,self.request)
        self.args=dict(registry=self.registry,spec=self.spec,request=self.request,attempt_id=self.attempt,
            datasets=self.data,views=self.views,strategy=EMACrossover(),config=approved_configuration(),rules_snapshot=self.snapshot)

    def validate(self,**changes):
        with patch('research.runner.RULES_HASH',self.rule_hash):
            return validate_inputs(**dict(self.args,**changes))

    def test_exact_authorized_identity_and_determinism(self):
        _,_,a=self.validate();_,_,b=self.validate()
        self.assertEqual(a,b)
        self.assertEqual(a['run_fingerprint'],self.registry.events()[-1]['payload']['run_id'])

    def test_unregistered_and_missing_attempt_denied(self):
        with self.assertRaises(ValueError):self.validate(registry=Registry(Path(self.temp.name)/'empty'))
        with self.assertRaises(PermissionError):self.validate(attempt_id='f'*64)

    def test_cost_source_version_and_strategy_mismatch(self):
        from research.strategy import EMAConfig
        for changes in (dict(config=replace(approved_configuration(),entry_fee_rate=D('.001'))),
                        dict(strategy=EMACrossover(EMAConfig(8,21))),dict(strategy=None)):
            with self.assertRaises(PermissionError):self.validate(**changes)
        with patch('research.runner.software_identity',return_value=(('fake','1'),)):
            with self.assertRaises(PermissionError):self.validate()

    def test_split_denial_precedes_data_inspection(self):
        for role,start,end in ((Role.VALIDATION,VALIDATION_START,HOLDOUT_START),(Role.HOLDOUT,HOLDOUT_START,HOLDOUT_START+STEP_MS)):
            req=SplitRequest(role,start,end,start-99*STEP_MS)
            with self.assertRaises(PermissionError):self.validate(request=req,datasets=None,views=None)

    def test_changed_view_rules_and_candle_rejected(self):
        with self.assertRaises(ValueError):self.validate(rules_snapshot={'rules_fingerprint':'0'*64})
        views=dict(self.views);views['ETHUSDT']=views['BTCUSDT']
        with self.assertRaises(ValueError):self.validate(views=views)
        data=dict(self.data);data['BTCUSDT']=replace(data['BTCUSDT'],candles=data['BTCUSDT'].candles[1:])
        with self.assertRaises(ValueError):self.validate(datasets=data)

    def test_three_plans_do_not_register_or_execute(self):
        before=self.registry.events()
        batch=planned_batch({tf:{s:'a'*64 for s in SYMBOLS} for tf in ('15m','1h','4h')},
            source_hashes=source_identity(),software_versions=software_identity())
        self.assertEqual(len({s.id for s in batch}),3)
        self.assertEqual(self.registry.events(),before)
        with self.assertRaises(ValueError):planned_batch({},source_hashes=(),software_versions=())

    def test_failure_retry_does_not_inflate_trials(self):
        bad=dict(self.args,config=replace(approved_configuration(),entry_fee_rate=D('.001')),
                 artifact_root=Path(self.temp.name)/'research')
        with self.assertRaises(PermissionError):execute(**bad)
        again=self.registry.begin_attempt(self.spec.id,self.request,self.spec.development_dataset_fingerprints)
        self.assertNotEqual(again,self.attempt)
        self.assertEqual(sum(e['kind']=='EXPERIMENT' for e in self.registry.events()),1)
        self.assertEqual([e['payload']['run_id'] for e in self.registry.events() if e['kind']=='ATTEMPT'],
                         [self.registry.events()[-1]['payload']['run_id']]*2)
        summary=ledger_summary(self.registry)
        self.assertEqual((summary['planned_configurations'],summary['attempts'],summary['failures'],summary['retries']),(1,2,1,1))

    def test_small_synthetic_schedule_financial_integration(self):
        schedule,rules,_=self.validate()
        original=HistoricalClock.__iter__
        # Test-only bounded clock: 3 flat synthetic boundaries, no full-year experiment.
        with patch.object(HistoricalClock,'__iter__',lambda clock:islice(original(clock),3)):
            result=simulate_portfolio(self.data,rules,approved_configuration(),decision_provider=schedule)
        self.assertEqual(len(result.boundaries),3)
        self.assertEqual(len(result.boundaries[0].decisions),3)
        self.assertEqual(result.config,approved_configuration())
        self.assertGreaterEqual(len(result.trades),1)
        self.assertEqual(result.trades[0].reason.value,'STOP_LOSS')
        self.assertEqual(result.boundaries[0].priority_winner,'BTCUSDT')

    def test_artifacts_preserve_first_result_and_terminal_outcome(self):
        data=ph.datasets();result=simulate_portfolio(data,ph.rules(),approved_configuration())
        args=dict(self.args,datasets=data,artifact_root=Path(self.temp.name)/'research')
        with patch('research.runner.validate_inputs',return_value=(None,ph.rules(),{'experiment_fingerprint':self.spec.id})),patch('research.runner.simulate_portfolio',return_value=result) as simulation:
            card=execute(**args)
            path=args['artifact_root']/self.spec.id/'development'
            saved=(path/'first-result.json').read_bytes()
            with self.assertRaises(FileExistsError):execute(**args)
            self.assertEqual(simulation.call_count,1)
            self.assertEqual(saved,(path/'first-result.json').read_bytes())
            self.assertEqual(json.loads((path/'scorecard.json').read_text()),card)
        with self.assertRaises(ValueError):self.registry.begin_attempt(self.spec.id,self.request,self.spec.development_dataset_fingerprints)

    def test_reporting_failure_keeps_checkpoint(self):
        data=ph.datasets();result=simulate_portfolio(data,ph.rules(),approved_configuration())
        args=dict(self.args,datasets=data,artifact_root=Path(self.temp.name)/'research')
        with patch('research.runner.validate_inputs',return_value=(None,ph.rules(),{})),patch('research.runner.simulate_portfolio',return_value=result),patch('research.runner.build_scorecard',side_effect=ValueError('test')):
            with self.assertRaises(ValueError):execute(**args)
        self.assertTrue((args['artifact_root']/self.spec.id/'development'/'first-result.json').exists())
        self.assertEqual(self.registry.events()[-1]['payload']['status'],'FAILED')
