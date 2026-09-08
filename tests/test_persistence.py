"""Temporary local databases only. No Binance imports, credentials, or calls."""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import config
import persistence
from errors import (CriticalError, PersistenceConflictError, PersistenceUnavailableError,
                    RuntimeOwnershipError, SchemaMismatchError, classify_error)
from persistence import Persistence, decimal_from_text, decimal_to_text, make_signal_key


class DecimalTests(unittest.TestCase):
    def test_canonical_round_trip(self):
        cases = {"0.000": "0", "-0.00": "0", "1.23000": "1.23", "1E+3": "1000",
                 "1E-12": "0.000000000001", "-12.340": "-12.34", "100.00": "100"}
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(decimal_to_text(Decimal(original)), expected)
                self.assertEqual(decimal_from_text(expected), Decimal(original))

    def test_serialization_never_uses_context_rounding(self):
        value = Decimal("12345678901234567890.12345678901234567890123456789")
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(decimal_from_text(decimal_to_text(value)), value)

    def test_nonfinite_and_noncanonical_values_rejected(self):
        for value in (1.5, 1, "1", True, Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal_to_text(value)
        for value in (None, 1, "1.0", "01", "+1", "-0", "1e3", " 1", "NaN", "Infinity", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal_from_text(value)

    def test_signal_key_is_unambiguous_and_includes_all_identity_fields(self):
        key = make_signal_key("ema-9/21-v1", "BTCUSDT", "15m", 100)
        self.assertEqual(key, '["ema-9/21-v1","BTCUSDT","15m",100]')
        for args in (("v2", "BTCUSDT", "15m", 100), ("ema-9/21-v1", "ETHUSDT", "15m", 100),
                     ("ema-9/21-v1", "BTCUSDT", "1m", 100), ("ema-9/21-v1", "BTCUSDT", "15m", 101)):
            self.assertNotEqual(make_signal_key(*args), key)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "bot.sqlite3"
        self.store = Persistence(self.path)
        self.store.begin_run(run_id="run-1", started_at_ms=100, baseline_commit="baseline", config={"testnet": True})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self, **kwargs):
        self.store.close()
        self.store = Persistence(self.path, **kwargs)

    def decision(self, identity="d1", symbol="BTCUSDT", candle=100, **overrides):
        args = dict(decision_id=identity, run_id="run-1", evaluated_at_ms=101, symbol=symbol,
                    timeframe="15m", candle_open_ms=candle, strategy_version="ema-9/21-v1",
                    signal="LONG", outcome="APPROVED", reason_codes=("APPROVED",),
                    snapshot={"price": Decimal("100.00")}, proposed_plan={"quantity": Decimal("0.01")})
        args.update(overrides)
        return self.store.create_decision(**args)

    def trade(self, identity="t1", decision="d1", **overrides):
        args = dict(run_id="run-1", trade_id=identity, decision_id=decision, created_at_ms=102,
                    plan={"planned_qty": Decimal("0.01"), "planned_sl_price": Decimal("98.5"),
                          "planned_tp_price": Decimal("103"), "approved_risk_usdt": Decimal("1")}, leverage=3)
        args.update(overrides)
        return self.store.create_trade_intent(**args)

    def order(self, identity="o1", **overrides):
        args = dict(run_id="run-1", local_order_id=identity, trade_id="t1", role="ENTRY", revision=0,
                    api_family="STANDARD", client_order_id="client-" + identity, side="BUY", order_type="MARKET",
                    created_at_ms=103, quantity=Decimal("0.01"), request={"symbol": "BTCUSDT"})
        args.update(overrides)
        return self.store.create_order_intent(**args)

    def update_trade(self, state, expected="INTENT", **observations):
        return self.store.update_trade_state("t1", run_id="run-1", expected_state=expected, state=state,
            updated_at_ms=110, evidence={"source": "synthetic test observation"}, observations=observations)

    def update_order(self, state, expected="INTENT", identity="o1", **observations):
        return self.store.update_order_state(identity, run_id="run-1", expected_state=expected, state=state,
            updated_at_ms=110, evidence={"source": "synthetic test observation"}, observations=observations)

    def test_creation_schema_and_connection_durability_settings(self):
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.store.validate_schema_version(), 1)
        for pragma, expected in (("foreign_keys", 1), ("journal_mode", "wal"), ("busy_timeout", 5000), ("synchronous", 2)):
            with self.subTest(pragma=pragma):
                self.assertEqual(self.store._connection.execute("PRAGMA " + pragma).fetchone()[0], expected)
        tables = {row[0] for row in self.store._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"runs", "decisions", "trades", "orders", "bot_state", "events"})

    def test_reopen_does_not_rerun_migration_or_overwrite_run(self):
        before = self.store.events()
        self.reopen(busy_timeout_ms=1234)
        self.assertEqual(self.store.events(), before)
        self.assertIsNone(self.store.get_run("run-1")["stopped_at_ms"])
        self.assertEqual(self.store._connection.execute("PRAGMA busy_timeout").fetchone()[0], 1234)

    def test_run_closure_is_explicit_and_atomic(self):
        self.store.end_run("run-1", stopped_at_ms=120, exit_reason="normal")
        self.assertEqual(self.store.get_run("run-1")["exit_reason"], "normal")
        self.assertEqual(self.store.events()[-1]["event_type"], "run_stopped")
        with self.assertRaises(PersistenceConflictError):
            self.store.end_run("run-1", stopped_at_ms=121, exit_reason="duplicate")
        with self.assertRaises(PersistenceConflictError):
            self.store.begin_run(run_id="run-1", started_at_ms=200, baseline_commit="new", config={})

    def test_decision_insertion_and_none_without_candle(self):
        decision = self.decision()
        self.assertEqual(json.loads(decision["snapshot_json"]), {"price": "100"})
        none = self.decision("d2", candle=None, signal="NONE", outcome="NO_SIGNAL", proposed_plan=None)
        self.assertIsNone(none["proposed_plan_json"])
        self.assertIsNone(none["candle_open_ms"])
        with self.assertRaises(PersistenceConflictError):
            self.trade(decision="d2")

    def test_foreign_keys_and_watchlist_enforced(self):
        for changes in ({"run_id": "missing"}, {"symbol": "XRPUSDT"}, {"timeframe": "1m"}):
            with self.subTest(changes=changes), self.assertRaises(PersistenceConflictError):
                self.decision(**changes)
        self.assertEqual(len(self.store.events()), 1)

    def test_trade_reservation_contains_no_invented_exchange_values(self):
        self.decision()
        trade = self.trade()
        self.assertEqual(trade["state"], "INTENT")
        self.assertEqual(trade["planned_qty"], "0.01")
        self.assertEqual(trade["accounting_complete"], 0)
        for key in ("filled_entry_qty", "remaining_qty", "avg_entry_price", "avg_exit_price", "opened_at_ms",
                    "closed_at_ms", "gross_pnl_usdt", "net_pnl_usdt", "actual_sl_price", "actual_tp_price"):
            self.assertIsNone(trade[key])
        self.assertEqual(self.store.active_trade()["trade_id"], "t1")

    def test_duplicate_signal_is_consumed_even_after_abort_and_restart(self):
        self.decision()
        first = self.trade()
        self.update_trade("ABORTED")
        self.reopen()
        self.decision("d2")
        self.assertTrue(self.store.signal_consumed(first["signal_key"]))
        with self.assertRaises(PersistenceConflictError):
            self.trade("t2", "d2")
        self.assertIsNone(self.store.active_trade())

    def test_only_one_active_trade_total_across_all_symbols(self):
        self.decision()
        self.trade()
        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "ADAUSDT"), 2):
            self.decision("d" + str(index), symbol, candle=index)
            with self.subTest(symbol=symbol), self.assertRaises(PersistenceConflictError):
                self.trade("t" + str(index), "d" + str(index))
        self.update_trade("ENTRY_UNKNOWN")
        with self.assertRaises(PersistenceConflictError):
            self.trade("t4", "d4")

    def test_order_intent_identity_and_unknown_fields(self):
        self.decision()
        self.trade()
        order = self.order()
        self.assertEqual(order["state"], "INTENT")
        self.assertEqual(order["position_side"], "BOTH")
        self.assertEqual(order["symbol"], "BTCUSDT")
        for field in ("exchange_order_id", "executed_qty", "avg_fill_price", "exchange_status", "last_response_json"):
            self.assertIsNone(order[field])
        self.assertEqual(self.store.unresolved_orders()[0]["local_order_id"], "o1")

    def test_client_id_and_role_revision_uniqueness(self):
        self.decision()
        self.trade()
        self.order()
        with self.assertRaises(PersistenceConflictError):
            self.order("o2", role="SL", client_order_id="client-o1")
        with self.assertRaises(PersistenceConflictError):
            self.order("o2")
        self.order("o2", revision=1)
        self.assertEqual(len(self.store.unresolved_orders()), 2)

    def test_exchange_identity_is_unique_and_cannot_be_reassigned(self):
        self.decision()
        self.trade()
        self.order()
        self.order("o2", role="EXIT")
        self.update_order("NEW", exchange_order_id="123")
        with self.assertRaises(PersistenceConflictError):
            self.update_order("NEW", identity="o2", exchange_order_id="123")
        with self.assertRaises(PersistenceConflictError):
            self.update_order("NEW", expected="NEW", exchange_order_id="456")
        # Distinct exchange namespaces may legitimately reuse a numeric ID.
        self.order("o3", role="SL", api_family="ALGO", quantity=None, close_position=True)
        self.update_order("NEW", identity="o3", exchange_order_id="123")

    def test_order_symbol_fk_cannot_disagree_with_trade(self):
        self.decision()
        self.trade()
        self.order()
        with self.assertRaises(PersistenceConflictError), self.store.transaction():
            self.store._connection.execute("UPDATE orders SET symbol='ETHUSDT' WHERE local_order_id='o1'")

    def test_close_all_parameter_conflicts_rejected(self):
        self.decision()
        self.trade()
        with self.assertRaises(PersistenceConflictError):
            self.order(close_position=True)
        with self.assertRaises(PersistenceConflictError):
            self.order(quantity=None, close_position=True, reduce_only=True)

    def test_state_updates_are_explicit_audited_and_compare_expected_state(self):
        self.decision()
        self.trade()
        self.order()
        self.update_order("SUBMITTING")
        self.update_order("UNKNOWN", expected="SUBMITTING")
        self.update_trade("ENTRY_UNKNOWN")
        self.reopen()
        self.assertEqual(self.store.active_trade()["state"], "ENTRY_UNKNOWN")
        self.assertEqual(self.store.unresolved_orders()[0]["state"], "UNKNOWN")
        with self.assertRaises(PersistenceConflictError):
            self.update_order("NEW")
        with self.assertRaises(PersistenceConflictError):
            self.update_order("NOT_A_STATE", expected="UNKNOWN")
        self.assertIsNone(self.store.get_order("o1")["executed_qty"])
        self.assertEqual(json.loads(self.store.events()[-1]["payload_json"])["to"], "ENTRY_UNKNOWN")

    def test_unresolved_order_prevents_releasing_trade_slot(self):
        self.decision()
        self.trade()
        self.order()
        with self.assertRaises(PersistenceConflictError):
            self.update_trade("ABORTED")
        with self.assertRaises(PersistenceConflictError):
            self.update_trade("CLOSED", remaining_qty=Decimal("0"), closed_at_ms=110)
        self.update_order("ABORTED")
        self.update_trade("ABORTED")
        self.assertIsNone(self.store.active_trade())
        self.assertEqual(self.store.unresolved_orders(), [])

    def test_unknown_order_cannot_be_locally_aborted(self):
        self.decision()
        self.trade()
        self.order()
        self.update_order("UNKNOWN")
        with self.assertRaises(PersistenceConflictError):
            self.update_order("ABORTED", expected="UNKNOWN")

    def test_trade_close_requires_explicit_flat_observation(self):
        self.decision()
        self.trade()
        self.update_trade("ENTRY_UNKNOWN")
        with self.assertRaises(PersistenceConflictError):
            self.update_trade("ABORTED", expected="ENTRY_UNKNOWN")
        for observations in ({}, {"remaining_qty": Decimal("0")}, {"remaining_qty": Decimal("1"), "closed_at_ms": 110}):
            with self.subTest(observations=observations), self.assertRaises(PersistenceConflictError):
                self.update_trade("CLOSED", expected="ENTRY_UNKNOWN", **observations)
        self.update_trade("CLOSED", expected="ENTRY_UNKNOWN", remaining_qty=Decimal("0"), closed_at_ms=110)
        self.assertIsNone(self.store.get_trade("t1")["net_pnl_usdt"])
        with self.assertRaises(PersistenceConflictError):
            self.update_trade("INTENT", expected="CLOSED")
        with self.assertRaises(PersistenceConflictError):
            self.order()

    def test_terminal_order_cannot_reopen_and_trigger_link_is_preserved(self):
        self.decision()
        self.trade()
        self.order()
        self.update_order("FILLED", executed_qty=Decimal("0.01"), avg_fill_price=Decimal("100.50"),
                          exchange_order_id="id-1", triggered_order_id="child-1", last_response_json={"status": "FILLED"})
        self.assertEqual(self.store.unresolved_orders(), [])
        with self.assertRaises(PersistenceConflictError):
            self.update_order("NEW", expected="FILLED")
        self.assertEqual(self.store.get_order("o1")["triggered_order_id"], "child-1")

    def test_financial_columns_store_text_and_reject_float_input(self):
        self.decision()
        self.trade()
        self.order()
        self.update_trade("PROTECTING", filled_entry_qty=Decimal("0.0100"), remaining_qty=Decimal("0.01"),
                          avg_entry_price=Decimal("100.50"), gross_pnl_usdt=Decimal("-1.20"))
        stored = self.store.get_trade("t1")
        self.assertEqual(stored["gross_pnl_usdt"], "-1.2")
        self.assertEqual(self.store._connection.execute("SELECT typeof(avg_entry_price) FROM trades").fetchone()[0], "text")
        for value in (1, 1.5, "1.5", Decimal("Infinity")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.update_trade("PROTECTING", expected="PROTECTING", remaining_qty=value)

    def test_atomic_reservation_and_entry_intent(self):
        self.decision()
        trade, order = self.store.reserve_entry(run_id="run-1",
            trade=dict(trade_id="t1", decision_id="d1", created_at_ms=102),
            order=dict(local_order_id="o1", revision=0, api_family="STANDARD", client_order_id="client-o1",
                       side="BUY", order_type="MARKET", created_at_ms=103, quantity=Decimal("0.01")))
        self.reopen()
        self.assertEqual(self.store.get_trade("t1"), trade)
        self.assertEqual(self.store.get_order("o1"), order)
        self.assertEqual([event["event_type"] for event in self.store.events()][-2:], ["trade_reserved", "order_intent_created"])

    def test_failed_entry_intent_rolls_back_reservation_and_events(self):
        self.decision()
        before = self.store.events()
        with self.assertRaises(PersistenceConflictError):
            self.store.reserve_entry(run_id="run-1",
                trade=dict(trade_id="t1", decision_id="d1", created_at_ms=102),
                order=dict(local_order_id="o1", revision=0, api_family="INVALID", client_order_id="client-o1",
                           side="BUY", order_type="MARKET", created_at_ms=103))
        self.assertIsNone(self.store.active_trade())
        self.assertEqual(self.store.events(), before)
        self.assertFalse(self.store.signal_consumed(make_signal_key("ema-9/21-v1", "BTCUSDT", "15m", 100)))

    def test_outer_transaction_rollback_including_successful_nested_methods(self):
        before = self.store.events()
        with self.assertRaises(RuntimeError), self.store.transaction():
            self.decision()
            self.trade()
            raise RuntimeError("synthetic failure")
        self.assertEqual(self.store.events(), before)
        self.assertIsNone(self.store.active_trade())
        with self.assertRaises(PersistenceConflictError):
            self.store.get_decision("d1")

    def test_event_failure_rolls_back_state_change(self):
        before = self.store.events()
        with self.assertRaises(RuntimeError), patch.object(self.store, "_audit", side_effect=RuntimeError("synthetic")):
            self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=110,
                                        changes={"entries_enabled": True}, reason="test")
        self.assertEqual(self.store.read_bot_state()["version"], 0)
        self.assertEqual(self.store.read_bot_state()["entries_enabled"], 0)
        self.assertEqual(self.store.events(), before)

    def test_events_are_append_only_even_for_replace(self):
        original = self.store.events()[0]
        for sql in ("UPDATE events SET severity='DEBUG'", "DELETE FROM events",
                    "INSERT OR REPLACE INTO events SELECT * FROM events WHERE event_id=1"):
            with self.subTest(sql=sql), self.assertRaises(PersistenceConflictError), self.store.transaction():
                self.store._connection.execute(sql)
        self.assertEqual(self.store.events(), [original])

    def test_runtime_flags_latch_and_loss_count_survive_reopen(self):
        state = self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=110,
            changes={"entries_enabled": True, "critical_latched": True, "critical_reason": "UNKNOWN_SUBMISSION",
                     "consecutive_losses": 2, "last_reconciled_at_ms": 105}, reason="synthetic uncertainty")
        self.reopen(trading_enabled_ceiling=True)
        self.assertEqual(self.store.read_bot_state(), state)
        self.assertFalse(self.store.entry_switch_enabled())
        self.assertIsNone(self.store.get_run("run-1")["stopped_at_ms"])

    def test_startup_ceiling_never_overwrites_durable_switch(self):
        self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=110,
                                    changes={"entries_enabled": True}, reason="explicit local request")
        self.assertFalse(self.store.entry_switch_enabled())
        self.reopen(trading_enabled_ceiling=True)
        self.assertTrue(self.store.entry_switch_enabled())
        self.reopen()
        self.assertEqual(self.store.read_bot_state()["entries_enabled"], 1)
        self.assertFalse(self.store.entry_switch_enabled())

    def test_explicit_disable_and_critical_clear_require_version_and_acknowledgement(self):
        self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=110,
            changes={"critical_latched": True, "critical_reason": "unknown"}, reason="test")
        with self.assertRaises(PersistenceConflictError):
            self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=111,
                                        changes={"entries_enabled": True}, reason="stale")
        with self.assertRaises(PersistenceConflictError):
            self.store.update_bot_state(run_id="run-1", expected_version=1, updated_at_ms=111,
                                        changes={"critical_latched": False}, reason="no acknowledgement")
        self.store.update_bot_state(run_id="run-1", expected_version=1, updated_at_ms=111,
            changes={"critical_latched": False, "critical_reason": None, "entries_enabled": False},
            reason="explicit resolution", acknowledge_critical=True)
        self.reopen(trading_enabled_ceiling=True)
        self.assertFalse(self.store.entry_switch_enabled())
        self.assertEqual(self.store.read_bot_state()["version"], 2)

    def test_runtime_latch_requires_reason_and_cannot_add_second_singleton(self):
        with self.assertRaises(PersistenceConflictError):
            self.store.update_bot_state(run_id="run-1", expected_version=0, updated_at_ms=110,
                                        changes={"critical_latched": True}, reason="test")
        with self.assertRaises(PersistenceConflictError), self.store.transaction():
            self.store._connection.execute("INSERT INTO bot_state VALUES(2,0,0,NULL,0,NULL,0,0)")

    def test_json_scrubs_secrets_but_preserves_order_identifiers(self):
        self.reopen(secrets=("synthetic-secret",))
        settings = config.Settings()
        self.store.begin_run(run_id="run-2", started_at_ms=120, baseline_commit="b" * 40,
            config={**{field.name: getattr(settings, field.name) for field in fields(settings)}, "API_SECRET": "private"})
        snapshot = json.loads(self.store.get_run("run-2")["config_json"])
        self.assertEqual(snapshot["RISK_PER_TRADE_PCT"], "1")
        self.assertEqual(snapshot["API_SECRET"], "[REDACTED]")
        self.store.append_event(run_id="run-2", occurred_at_ms=121, event_type="test",
            payload={"client_order_id": "a" * 36, "nested": {"api_key": "private"}, "message": "synthetic-secret"})
        payload = json.loads(self.store.events()[-1]["payload_json"])
        self.assertEqual(payload["client_order_id"], "a" * 36)
        self.assertEqual(payload["message"], "[REDACTED]")

    def test_closed_store_fails_critically(self):
        self.store.close()
        with self.assertRaises(PersistenceUnavailableError):
            self.store.read_bot_state()

    def test_second_owner_is_rejected_without_overwriting_state(self):
        before = self.store.read_bot_state()
        with self.assertRaises(RuntimeOwnershipError):
            Persistence(self.path)
        self.assertEqual(self.store.read_bot_state(), before)

    def test_schema_version_mismatch_fails_closed(self):
        self.store._connection.execute("PRAGMA user_version=99")
        with self.assertRaises(SchemaMismatchError):
            self.store.validate_schema_version()
        with self.assertRaises(PersistenceUnavailableError):
            self.store.read_bot_state()
        self.store.close()
        with self.assertRaises(SchemaMismatchError):
            Persistence(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_schema_tampering_at_same_version_is_rejected(self):
        self.store._connection.execute("DROP INDEX one_active_trade_total")
        self.store.close()
        with self.assertRaises(SchemaMismatchError):
            Persistence(self.path)

    def test_failed_database_write_is_critical_and_rolls_back(self):
        self.store._connection.execute("PRAGMA query_only=ON")
        with self.assertRaises(PersistenceUnavailableError):
            self.decision()
        with self.assertRaises(PersistenceUnavailableError):
            self.store.read_bot_state()

    def test_missing_bot_state_is_not_silently_recreated(self):
        self.store._connection.execute("DELETE FROM bot_state")
        self.store.close()
        with self.assertRaises(PersistenceUnavailableError):
            Persistence(self.path)

    def test_error_classes_are_critical(self):
        for kind in (PersistenceUnavailableError, PersistenceConflictError, SchemaMismatchError, RuntimeOwnershipError):
            error = kind("synthetic")
            self.assertIsInstance(error, CriticalError)
            self.assertFalse(classify_error(error).recoverable)
            self.assertTrue(classify_error(error).blocks_entries)


class DatabaseFailureTests(unittest.TestCase):
    def test_corrupted_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrupt.sqlite3"
            original = b"not a database: synthetic-secret"
            path.write_bytes(original)
            with self.assertRaises(PersistenceUnavailableError) as caught:
                Persistence(path)
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertEqual(path.read_bytes(), original)

    def test_unavailable_parent_is_critical(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "file"
            parent.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(PersistenceUnavailableError):
                Persistence(parent / "bot.sqlite3")

    def test_nonempty_unversioned_database_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE original (value TEXT)")
            with self.assertRaises(SchemaMismatchError):
                Persistence(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='original'").fetchone())

    def test_migration_failure_is_atomic_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bot.sqlite3"
            real_connect = sqlite3.connect

            def fail_one_statement(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                if str(args[0]) != ":memory:":
                    connection.set_authorizer(lambda action, first, *rest:
                        sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and first == "orders" else sqlite3.SQLITE_OK)
                return connection

            with patch.object(persistence.sqlite3, "connect", side_effect=fail_one_statement):
                with self.assertRaises(PersistenceUnavailableError):
                    Persistence(path)
            with closing(real_connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
            with Persistence(path) as store:
                self.assertEqual(store.validate_schema_version(), 1)
