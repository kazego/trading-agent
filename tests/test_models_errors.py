import unittest
from dataclasses import FrozenInstanceError

import errors
from models import OrderRole, PositionDirection, RiskResult, RuntimeState, Signal, SnapshotRef


class ModelTests(unittest.TestCase):
    def test_enums_and_external_signal_conversion(self):
        self.assertEqual([item.value for item in Signal], ["LONG", "SHORT", "NONE"])
        self.assertEqual(Signal("LONG"), "LONG")
        self.assertEqual([item.value for item in OrderRole], ["ENTRY", "SL", "TP", "EXIT"])
        self.assertEqual([item.value for item in PositionDirection], ["LONG", "SHORT"])
        self.assertEqual([item.value for item in RuntimeState],
                         ["STARTING", "RECONCILING", "READY", "DEGRADED", "HALTED", "STOPPING"])
        with self.assertRaises(ValueError):
            Signal("BUY")
        with self.assertRaises(ValueError):
            PositionDirection("NONE")

    def test_risk_result_preserves_reasons_and_snapshot(self):
        snapshot = SnapshotRef("account-1", 1788825600000)
        for approved, reason in ((True, "APPROVED"), (False, "STALE_DATA")):
            result = RiskResult(approved, (reason,), snapshot)
            self.assertIs(result.snapshot, snapshot)
            self.assertEqual(result.reason_codes, (reason,))
            with self.assertRaises(FrozenInstanceError):
                result.approved = not approved
        self.assertIsNone(snapshot.symbol)
        self.assertEqual(SnapshotRef("market-1", 0, "ADAUSDT").symbol, "ADAUSDT")

    def test_models_reject_missing_or_mutable_contract_fields(self):
        for args in (("", 0), ("x", -1), ("x", True), ("x", 1.5), ("x", 0, "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SnapshotRef(*args)
        snapshot = SnapshotRef("x", 0)
        for args in ((True, (), snapshot), (False, ["BLOCKED"], snapshot),
                     (True, ("",), snapshot), (1, ("OK",), snapshot), (True, ("OK",), None)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                RiskResult(*args)


class ErrorTests(unittest.TestCase):
    def test_explicit_read_errors_are_recoverable_but_block_stale_entries(self):
        for kind in (errors.TransientReadError, errors.ReadTimeoutError, errors.RateLimitError):
            with self.subTest(kind=kind):
                classification = errors.classify_error(kind())
                self.assertTrue(classification.recoverable)
                self.assertTrue(classification.blocks_entries)
                self.assertIs(classification.action, errors.ErrorAction.RETRY_READ)

    def test_critical_hierarchy_and_actions(self):
        for kind in (errors.ConfigurationError, errors.AuthenticationError, errors.AccountModeError,
                     errors.PersistenceUnavailableError, errors.LoggingUnavailableError):
            with self.subTest(kind=kind):
                error = kind("synthetic")
                self.assertIsInstance(error, errors.CriticalError)
                classification = errors.classify_error(error)
                self.assertFalse(classification.recoverable)
                self.assertTrue(classification.blocks_entries)
                self.assertIs(classification.action, errors.ErrorAction.REQUIRE_OPERATOR)
        self.assertIs(errors.classify_error(errors.UnknownExchangeStateError()).action, errors.ErrorAction.RECONCILE)
        self.assertIs(errors.classify_error(errors.ProtectionFailureError()).action, errors.ErrorAction.PROTECT_POSITION)

    def test_unknown_and_generic_timeouts_are_not_automatically_recoverable(self):
        for error in (Exception(), ValueError(), TimeoutError(), OSError(), errors.ApplicationError()):
            with self.subTest(error=type(error)):
                classification = errors.classify_error(error)
                self.assertFalse(classification.recoverable)
                self.assertTrue(classification.blocks_entries)
                self.assertIs(classification.action, errors.ErrorAction.REQUIRE_OPERATOR)
