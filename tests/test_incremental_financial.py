import unittest
from unittest.mock import patch
from benchmark_incremental_financial import run
from research.incremental_ledger import SyntheticLedger, Blocked
from research.spec import encoded


class FinancialScenarios(unittest.TestCase):
    def test_entry_evidence_restored_and_conflicting_content_blocked(self):
        original = SyntheticLedger.apply
        checked = []
        def apply(core, kind, data, **kwargs):
            result = original(core, kind, data, **kwargs)
            if kind == 'ENTRY':
                state = core._state(core.external.anchor)
                rows = state.rows()['candles']
                self.assertEqual(len(rows), 1)
                digest, payload = rows[0]
                self.assertEqual(payload, encoded(data['candle']).decode())
                core._check_rows({'candles': rows})
                # Identical pre-state is recognized; conflicting immutable
                # content must still fail even with the same primary key.
                core._write_delta({'candles': rows}, {'candles': rows})
                with self.assertRaisesRegex(Blocked, 'Conflicting existing financial row'):
                    core._write_delta({'candles': rows}, {'candles': [(digest, '{}')]})
                core._check_rows({'candles': rows})
                checked.append(digest)
            return result
        with patch.object(SyntheticLedger, 'apply', apply):
            run(2, emit=lambda row: None)
        self.assertEqual(len(checked), 2)

    def test_multiple_trades_fees_and_retries(self):
        r=run(4,modes=('TP','TP','SL','SL'),emit=lambda row:None)
        self.assertEqual(r['closed_trades'],5)
        self.assertEqual(r['fills'],10)
        self.assertEqual(r['latency']['RETRY_CLOSED']['n'],4)
        self.assertEqual(r['latency']['CLOSE_SL']['n'],2)
        self.assertEqual(r['latency']['CLOSE_TP']['n'],2)
        self.assertEqual(r['slot'],'EMPTY');self.assertEqual(r['audit'],'PASS')

    def test_gap_stop_and_dual_touch_both_directions(self):
        r=run(4,modes=('DUAL','DUAL','GAP_SL','GAP_SL'),emit=lambda row:None)
        self.assertEqual(r['audit'],'PASS')
        self.assertEqual(r['closed_trades'],5)


if __name__=='__main__':unittest.main()
