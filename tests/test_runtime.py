"""Real local subprocess tests of ownership and abrupt-exit recovery."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from persistence import Persistence
from runtime import DatabaseLock


class RuntimeOwnershipTests(unittest.TestCase):
    def child(self, code, path):
        return subprocess.run([sys.executable, "-B", "-c", code, str(path)],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True,
                              text=True, timeout=20,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_second_process_cannot_acquire_ownership(self):
        code = """
import sys
from persistence import Persistence
from errors import RuntimeOwnershipError
try:
    with Persistence(sys.argv[1]):
        raise AssertionError('ownership was not exclusive')
except RuntimeOwnershipError:
    print('ownership rejected')
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.sqlite3"
            with Persistence(path) as store:
                before = store.read_bot_state()
                result = self.child(code, path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "ownership rejected")
                self.assertEqual(store.read_bot_state(), before)

    def test_abrupt_exit_releases_lock_and_preserves_committed_uncertainty(self):
        code = """
import os, sys
from persistence import Persistence
s = Persistence(sys.argv[1])
s.begin_run(run_id='crashed', started_at_ms=1, baseline_commit='baseline', config={})
s.create_decision(decision_id='d', run_id='crashed', evaluated_at_ms=2, symbol='ADAUSDT',
    timeframe='15m', candle_open_ms=0, strategy_version='v1', signal='LONG', outcome='APPROVED',
    reason_codes=['APPROVED'], snapshot={})
s.reserve_entry(run_id='crashed', trade=dict(trade_id='t', decision_id='d', created_at_ms=3),
    order=dict(local_order_id='o', revision=0, api_family='STANDARD', client_order_id='client-o',
               side='BUY', order_type='MARKET', created_at_ms=4))
s.update_order_state('o', run_id='crashed', expected_state='INTENT', state='UNKNOWN',
    updated_at_ms=5, evidence={'source':'synthetic uncertainty'})
s.update_trade_state('t', run_id='crashed', expected_state='INTENT', state='ENTRY_UNKNOWN',
    updated_at_ms=5, evidence={'source':'synthetic uncertainty'})
s.update_bot_state(run_id='crashed', expected_version=0, updated_at_ms=6,
    changes={'entries_enabled':True, 'critical_latched':True, 'critical_reason':'UNKNOWN'}, reason='test')
with s.transaction():
    s.append_event(run_id='crashed', occurred_at_ms=7, event_type='uncommitted', payload={})
    os._exit(0)
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.sqlite3"
            result = self.child(code, path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with Persistence(path, trading_enabled_ceiling=True) as store:
                self.assertIsNone(store.get_run("crashed")["stopped_at_ms"])
                self.assertEqual(store.active_trade()["state"], "ENTRY_UNKNOWN")
                self.assertEqual(store.unresolved_orders()[0]["state"], "UNKNOWN")
                self.assertIsNone(store.get_order("o")["exchange_order_id"])
                self.assertEqual(store.read_bot_state()["entries_enabled"], 1)
                self.assertEqual(store.read_bot_state()["critical_latched"], 1)
                self.assertFalse(store.entry_switch_enabled())
                self.assertNotIn("uncommitted", [event["event_type"] for event in store.events()])

    def test_retained_lock_file_is_not_a_stale_process_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.sqlite3"
            first = DatabaseLock(path)
            first.acquire()
            first.release()
            self.assertTrue(first.path.is_file())
            second = DatabaseLock(path)
            second.acquire()
            second.release()
