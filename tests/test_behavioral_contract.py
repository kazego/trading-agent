"""Synthetic component probes only; no simulator or historical data loader."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research import behavioral_contract as b
from research import features, strategy, schedule, data, protection, runner
from backtest import simulation

ROOT=Path(__file__).resolve().parents[1]


class BehavioralContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract_id=b.fingerprint(b.contract(ROOT))
        cls.implementation=b.implementation_identity(ROOT)

    def verify(self):
        return b.verify(ROOT,expected_contract=self.contract_id,approved_implementation=self.implementation)

    def test_deterministic(self):
        self.assertEqual(self.verify(), self.contract_id)
        self.assertEqual(b.observe(), b.observe())

    def test_unused_constructor_parameter(self):
        original=schedule.ResearchSchedule.__init__
        def constructor(self, views, selected, *, context=None):
            return original(self,views,selected)
        with patch.object(schedule.ResearchSchedule,'__init__',constructor):
            self.assertEqual(self.verify(),self.contract_id)

    def test_constructor_rename_not_silent(self):
        with patch.object(schedule.ResearchSchedule,'__init__',object.__init__):
            with self.assertRaises(TypeError): self.verify()

    def test_ema_math(self):
        original=features.pd.Series.ewm
        def changed(series,*args,**kwargs):
            kwargs['adjust']=True
            return original(series,*args,**kwargs)
        with patch.object(features.pd.Series,'ewm',changed):
            with self.assertRaises(ValueError): self.verify()

    def test_atr_math(self):
        original=features._atr
        with patch.object(features,'_atr',side_effect=lambda cs: original(cs)+1):
            with self.assertRaises(ValueError): self.verify()

    def test_crossover(self):
        original=strategy.EMACrossover.evaluate
        with patch.object(strategy.EMACrossover,'evaluate',side_effect=None,autospec=True) as mock:
            mock.side_effect=lambda self,w: replace(original(self,w),signal='NONE')
            with self.assertRaises(ValueError): self.verify()

    def test_schedule(self):
        original=schedule.ResearchSchedule.window_at
        def changed(self,symbol,t):
            return original(self,symbol,t) or original(self,symbol,b.DEV_START)
        with patch.object(schedule.ResearchSchedule,'window_at',changed):
            with self.assertRaises(ValueError): self.verify()

    def test_aggregation(self):
        original=data.aggregate_bucket
        with patch.object(data,'aggregate_bucket',side_effect=lambda cs,tf: replace(original(cs,tf),volume=original(cs,tf).volume+1)):
            with self.assertRaises(ValueError): self.verify()

    def test_ordering(self):
        original=data.aggregate_bucket
        with patch.object(data,'aggregate_bucket',side_effect=lambda cs,tf: original(tuple(sorted(cs,key=lambda c:c.open_time_ms)),tf)):
            with self.assertRaises(ValueError): self.verify()

    def test_protection(self):
        original=protection.ATRProtection.levels
        with patch.object(protection.ATRProtection,'levels',side_effect=lambda *a: tuple(x+1 for x in original(*a))):
            with self.assertRaises(ValueError): self.verify()

    def test_risk(self):
        original=simulation.size_quantity
        with patch.object(simulation,'size_quantity',side_effect=lambda *a,**kw: tuple(x*2 for x in original(*a,**kw))):
            with self.assertRaises(ValueError): self.verify()

    def test_cost_leverage(self):
        original=runner.approved_configuration
        for changes in (dict(leverage=b.D(2)),dict(slippage_bps=b.D(3)),dict(entry_fee_rate=b.D('0.001'))):
            with self.subTest(changes=changes), patch.object(runner,'approved_configuration',side_effect=lambda: replace(original(),**changes)):
                with self.assertRaises(ValueError): self.verify()

    def test_source_gate_covers_unobserved_changes(self):
        # Even a mutation outside finite fixture coverage invalidates the approved
        # implementation binding; golden matches cannot grant source approval.
        with patch.object(b,'implementation_identity',return_value='0'*64):
            with self.assertRaises(ValueError): self.verify()
            self.assertEqual(b.fingerprint(b.contract(ROOT)),self.contract_id)

    def test_missing_contract_components(self):
        for component in ('spec','golden'):
            record=b.contract(ROOT)
            record[component].pop(next(iter(record[component])))
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/b.FIXTURE_FILE
                path.parent.mkdir(parents=True)
                path.write_bytes(b.encoded(record)+b'\n')
                with self.assertRaises(ValueError): b.contract(directory)

    def test_historical_migration_read_only(self):
        ledger=ROOT/'artifacts/research/registry/events.jsonl'
        before=ledger.read_bytes()
        with patch.object(b.history.Registry,'_append',side_effect=AssertionError('No ledger writes')):
            result=b.migration_binding(ROOT,expected_contract=self.contract_id,approved_implementation=self.implementation)
        self.assertEqual(result['record']['identity2'],b.IDENTITY2)
        self.assertEqual(result['record']['contract'],self.contract_id)
        self.assertEqual(before,ledger.read_bytes())


if __name__=='__main__': unittest.main()
