"""Local durable memory only. No exchange calls or inference of exchange outcomes."""

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from errors import PersistenceConflictError, PersistenceUnavailableError, SchemaMismatchError
from runtime import DatabaseLock

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "001_initial.sql"
TRADE_TERMINAL = ("CLOSED", "ABORTED")
ORDER_TERMINAL = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "ABORTED")
TRADE_DECIMALS = frozenset((
    "planned_qty", "filled_entry_qty", "remaining_qty", "avg_entry_price", "avg_exit_price",
    "planned_sl_price", "planned_tp_price", "actual_sl_price", "actual_tp_price",
    "approved_risk_usdt", "gross_pnl_usdt", "fees_usdt", "funding_usdt", "net_pnl_usdt",
))
ORDER_DECIMALS = frozenset(("quantity", "trigger_price", "executed_qty", "avg_fill_price"))
_SENSITIVE = re.compile(r"api.?key|secret|password|passwd|token|authorization|signature|cookie|credential", re.I)


def decimal_to_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Financial values must be finite Decimal instances")
    if value == 0:
        return "0"
    text = format(value, "f")  # Does not round to the current Decimal context.
    return text.rstrip("0").rstrip(".") if "." in text else text


def decimal_from_text(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Stored decimal must be canonical text")
    try:
        result = Decimal(value)
        if decimal_to_text(result) == value:
            return result
    except (InvalidOperation, ValueError):
        pass
    raise ValueError("Stored decimal must be canonical text")


def _timestamp(value):
    if type(value) is not int or not 0 <= value <= 9223372036854775807:
        raise ValueError("Timestamp must be a nonnegative UTC epoch millisecond integer")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identifier must be nonempty text")
    return value


def make_signal_key(strategy_version, symbol, timeframe, candle_open_ms) -> str:
    return json.dumps([_identifier(strategy_version), _identifier(symbol), _identifier(timeframe),
                       _timestamp(candle_open_ms)], separators=(",", ":"), ensure_ascii=False)


class Persistence:
    """One owning store per local database. Opening never resets runtime flags.

    Methods commit their own local change plus event. transaction() can compose
    them atomically; nested calls use savepoints. Never do network I/O inside it.
    """

    def __init__(self, path, *, busy_timeout_ms=BUSY_TIMEOUT_MS,
                 trading_enabled_ceiling=False, secrets=()):
        if type(busy_timeout_ms) is not int or not 0 < busy_timeout_ms <= 2147483647:
            raise ValueError("busy_timeout_ms must be a positive 32-bit integer")
        if type(trading_enabled_ceiling) is not bool:
            raise ValueError("trading_enabled_ceiling must be boolean")
        self._connection = None
        self._lock = None
        self._failed = False
        self._savepoint = 0
        self._ceiling = trading_enabled_ceiling
        self._secrets = tuple(s for s in secrets if isinstance(s, str) and s)
        try:
            self.path = Path(path).resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = DatabaseLock(self.path)
            self._lock.acquire()
            self._connection = sqlite3.connect(self.path, isolation_level=None,
                                               timeout=busy_timeout_ms / 1000)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            migration = MIGRATION_PATH.read_text(encoding="utf-8")
            with closing(sqlite3.connect(":memory:")) as expected:
                expected.executescript(migration)
                self._expected_schema = self._manifest(expected)
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            empty = not self._manifest(self._connection)
            if version != SCHEMA_VERSION and not (version == 0 and empty):
                raise SchemaMismatchError("Unsupported database schema; no automatic reset or migration")
            if version == SCHEMA_VERSION:
                self.validate_schema_version()
            if self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                raise PersistenceUnavailableError("WAL could not be enabled")
            self._connection.execute("PRAGMA synchronous = FULL")
            if version == 0:
                self._connection.executescript(migration)
            self.validate_schema_version()
        except (sqlite3.Error, OSError):
            self.close()
            raise PersistenceUnavailableError("Local database initialization failed") from None
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _manifest(connection):
        return [(row[0], row[1], " ".join((row[2] or "").split())) for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]

    def _ready(self):
        if self._connection is None or self._failed:
            raise PersistenceUnavailableError("Local database is closed or failed")

    def validate_schema_version(self):
        self._ready()
        try:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION or self._manifest(self._connection) != self._expected_schema:
                self._failed = True
                raise SchemaMismatchError("Database schema does not match version 1")
            if (self._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None
                    or self._connection.execute("SELECT count(*) FROM bot_state").fetchone()[0] != 1):
                self._failed = True
                raise PersistenceUnavailableError("Database integrity check failed")
        except sqlite3.Error:
            self._failed = True
            raise PersistenceUnavailableError("Database validation failed") from None
        return version

    def close(self):
        try:
            if self._connection is not None:
                self._connection.close()  # Rolls back an unfinished transaction.
        finally:
            self._connection = None
            if self._lock is not None:
                self._lock.release()

    def __enter__(self):
        self._ready()
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self._ready()
        outer = not self._connection.in_transaction
        self._savepoint += 1
        name = f"local_{self._savepoint}"
        try:
            self._connection.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {name}")
            yield self
            self._connection.execute("COMMIT" if outer else f"RELEASE SAVEPOINT {name}")
        except BaseException as error:
            try:
                if self._connection.in_transaction:
                    if outer:
                        self._connection.execute("ROLLBACK")
                    else:
                        self._connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                        self._connection.execute(f"RELEASE SAVEPOINT {name}")
            except sqlite3.Error:
                self._failed = True
                raise PersistenceUnavailableError("Database rollback failed") from None
            if isinstance(error, sqlite3.IntegrityError):
                raise PersistenceConflictError("Database constraint rejected the local change") from None
            if isinstance(error, sqlite3.Error):
                self._failed = True
                raise PersistenceUnavailableError("Database transaction failed") from None
            raise

    def _rows(self, sql, parameters=()):
        self._ready()
        try:
            return [dict(row) for row in self._connection.execute(sql, parameters).fetchall()]
        except sqlite3.Error:
            self._failed = True
            raise PersistenceUnavailableError("Database read failed") from None

    def _one(self, table, key, value):
        rows = self._rows(f"SELECT * FROM {table} WHERE {key} = ?", (value,))
        if not rows:
            raise PersistenceConflictError("Referenced local record does not exist")
        return rows[0]

    def _json(self, value, *, allow_config_floats=False):
        def clean(item):
            if isinstance(item, Enum):
                return clean(item.value)
            if isinstance(item, Decimal):
                return decimal_to_text(item)
            if type(item) is float and allow_config_floats:
                return decimal_to_text(Decimal(str(item)))
            if isinstance(item, Path):
                return clean(str(item))
            if isinstance(item, str):
                for secret in sorted(self._secrets, key=len, reverse=True):
                    item = item.replace(secret, "[REDACTED]")
                return item
            if item is None or type(item) in (bool, int):
                return item
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON keys must be strings")
                return {clean(key): "[REDACTED]" if _SENSITIVE.search(key) else clean(val)
                        for key, val in item.items()}
            if isinstance(item, (list, tuple)):
                return [clean(val) for val in item]
            raise ValueError("JSON accepts Decimal values, not floats or arbitrary objects")
        return json.dumps(clean(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    def _insert(self, table, values):
        # Table and column names come only from internal fixed dictionaries.
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        return self._connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                                        tuple(values.values())).lastrowid

    def _audit(self, run_id, at_ms, event_type, payload, *, severity="INFO",
               decision_id=None, trade_id=None, local_order_id=None):
        return self._insert("events", dict(run_id=run_id, occurred_at_ms=_timestamp(at_ms),
            severity=severity, event_type=_identifier(event_type), decision_id=decision_id,
            trade_id=trade_id, local_order_id=local_order_id, payload_json=self._json(payload)))

    def append_event(self, *, run_id, occurred_at_ms, event_type, payload, severity="INFO",
                     decision_id=None, trade_id=None, local_order_id=None):
        with self.transaction():
            return self._audit(run_id, occurred_at_ms, event_type, payload, severity=severity,
                               decision_id=decision_id, trade_id=trade_id, local_order_id=local_order_id)

    def begin_run(self, *, run_id, started_at_ms, baseline_commit, config):
        with self.transaction():
            self._insert("runs", dict(run_id=_identifier(run_id), started_at_ms=_timestamp(started_at_ms),
                                      baseline_commit=_identifier(baseline_commit),
                                      config_json=self._json(config, allow_config_floats=True)))
            self._audit(run_id, started_at_ms, "run_started", {})
        return self.get_run(run_id)

    def end_run(self, run_id, *, stopped_at_ms, exit_reason):
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE runs SET stopped_at_ms=?, exit_reason=? WHERE run_id=? AND stopped_at_ms IS NULL",
                (_timestamp(stopped_at_ms), _identifier(exit_reason), run_id))
            if cursor.rowcount != 1:
                raise PersistenceConflictError("Run does not exist or was already stopped")
            self._audit(run_id, stopped_at_ms, "run_stopped", {"reason": exit_reason})

    def create_decision(self, *, decision_id, run_id, evaluated_at_ms, symbol, timeframe,
                        candle_open_ms, strategy_version, signal, outcome, reason_codes,
                        snapshot, proposed_plan=None):
        if not isinstance(reason_codes, (list, tuple)) or not reason_codes or not all(
                isinstance(reason, str) and reason.strip() for reason in reason_codes):
            raise ValueError("Decision requires reason codes")
        with self.transaction():
            self._insert("decisions", dict(decision_id=_identifier(decision_id), run_id=run_id,
                evaluated_at_ms=_timestamp(evaluated_at_ms), symbol=symbol, timeframe=timeframe,
                candle_open_ms=None if candle_open_ms is None else _timestamp(candle_open_ms),
                strategy_version=_identifier(strategy_version), signal=signal, outcome=outcome,
                reason_codes_json=self._json(reason_codes), snapshot_json=self._json(snapshot),
                proposed_plan_json=None if proposed_plan is None else self._json(proposed_plan)))
            self._audit(run_id, evaluated_at_ms, "decision_created", {"outcome": outcome}, decision_id=decision_id)
        return self.get_decision(decision_id)

    @staticmethod
    def _financial(values, allowed):
        if not isinstance(values, Mapping) or set(values) - allowed:
            raise ValueError("Unsupported financial fields")
        return {key: None if value is None else decimal_to_text(value) for key, value in values.items()}

    def create_trade_intent(self, *, run_id, trade_id, decision_id, created_at_ms, plan=None, leverage=None):
        allowed = frozenset(("planned_qty", "planned_sl_price", "planned_tp_price", "approved_risk_usdt"))
        values = self._financial({} if plan is None else plan, allowed)
        if leverage is not None and (type(leverage) is not int or leverage <= 0):
            raise ValueError("Leverage must be a positive integer")
        with self.transaction():
            decision = self.get_decision(decision_id)
            if decision["outcome"] != "APPROVED" or decision["signal"] not in ("LONG", "SHORT"):
                raise PersistenceConflictError("Trade intent requires an approved directional decision")
            key = make_signal_key(decision["strategy_version"], decision["symbol"],
                                  decision["timeframe"], decision["candle_open_ms"])
            self._insert("trades", dict(trade_id=_identifier(trade_id), decision_id=decision_id,
                signal_key=key, symbol=decision["symbol"], direction=decision["signal"], origin="BOT",
                state="INTENT", created_at_ms=_timestamp(created_at_ms), updated_at_ms=created_at_ms,
                leverage=leverage, **values))
            self._audit(run_id, created_at_ms, "trade_reserved", {"signal_key": key},
                        decision_id=decision_id, trade_id=trade_id)
        return self.get_trade(trade_id)

    def create_order_intent(self, *, run_id, local_order_id, trade_id, role, revision, api_family,
                            client_order_id, side, order_type, created_at_ms, quantity=None,
                            trigger_price=None, working_type=None, reduce_only=False,
                            close_position=False, request=None):
        if type(revision) is not int or revision < 0 or type(reduce_only) is not bool or type(close_position) is not bool:
            raise ValueError("Invalid order revision or boolean flags")
        values = self._financial({"quantity": quantity, "trigger_price": trigger_price}, ORDER_DECIMALS)
        with self.transaction():
            trade = self.get_trade(trade_id)
            self._insert("orders", dict(local_order_id=_identifier(local_order_id), trade_id=trade_id,
                symbol=trade["symbol"], role=role, revision=revision, api_family=api_family,
                client_order_id=_identifier(client_order_id), side=side, position_side="BOTH",
                order_type=_identifier(order_type), working_type=working_type, reduce_only=int(reduce_only),
                close_position=int(close_position), state="INTENT", created_at_ms=_timestamp(created_at_ms),
                updated_at_ms=created_at_ms, request_json=self._json({} if request is None else request), **values))
            self._audit(run_id, created_at_ms, "order_intent_created", {}, trade_id=trade_id, local_order_id=local_order_id)
        return self.get_order(local_order_id)

    def reserve_entry(self, *, run_id, trade, order):
        """Atomic reservation + ENTRY intent + events, before any future submission."""
        with self.transaction():
            reserved = self.create_trade_intent(run_id=run_id, **trade)
            intent = self.create_order_intent(run_id=run_id, trade_id=reserved["trade_id"], role="ENTRY", **order)
        return reserved, intent

    def _update_state(self, table, id_column, identity, *, run_id, expected_state, state,
                      updated_at_ms, evidence, observations=None):
        _timestamp(updated_at_ms)
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValueError("State updates require explicit caller evidence/reason")
        decimal_fields = TRADE_DECIMALS if table == "trades" else ORDER_DECIMALS
        other_fields = {"opened_at_ms", "closed_at_ms", "accounting_complete", "close_reason"} if table == "trades" else {
            "exchange_order_id", "triggered_order_id", "exchange_status", "last_response_json"}
        observations = {} if observations is None else dict(observations)
        if set(observations) - (decimal_fields | other_fields):
            raise ValueError("Unsupported observation fields")
        values = self._financial({key: val for key, val in observations.items() if key in decimal_fields}, decimal_fields)
        for key, value in observations.items():
            if key in decimal_fields:
                continue
            if key.endswith("_at_ms") and value is not None:
                value = _timestamp(value)
            elif key == "accounting_complete":
                if type(value) is not bool:
                    raise ValueError("accounting_complete must be boolean")
                value = int(value)
            elif key == "last_response_json" and value is not None:
                value = self._json(value)
            elif value is not None:
                value = _identifier(value)
            values[key] = value
        with self.transaction():
            current = self._one(table, id_column, identity)
            terminal = TRADE_TERMINAL if table == "trades" else ORDER_TERMINAL
            if current["state"] != expected_state or updated_at_ms < current["updated_at_ms"]:
                raise PersistenceConflictError("Stale state update rejected")
            if current["state"] in terminal and state != current["state"]:
                raise PersistenceConflictError("Terminal local state cannot be reopened")
            for key in ("exchange_order_id", "triggered_order_id"):
                if current.get(key) is not None and key in values and values[key] != current[key]:
                    raise PersistenceConflictError("Known exchange identity cannot be replaced")
            if table == "orders" and state == "ABORTED" and current["state"] not in ("INTENT", "ABORTED"):
                raise PersistenceConflictError("Only an unsubmitted local order intent can be aborted")
            requires_flat = state == "CLOSED" or (state == "ABORTED" and current["state"] not in ("INTENT", "ABORTED"))
            if table == "trades" and requires_flat and (
                    values.get("remaining_qty", current["remaining_qty"]) != "0"
                    or values.get("closed_at_ms", current["closed_at_ms"]) is None):
                raise PersistenceConflictError("Closing requires explicit flat quantity and closure time")
            values.update(state=state, updated_at_ms=_timestamp(updated_at_ms))
            assignments = ",".join(f"{key}=?" for key in values)
            self._connection.execute(f"UPDATE {table} SET {assignments} WHERE {id_column}=?",
                                     (*values.values(), identity))
            self._audit(run_id, updated_at_ms, table + "_state_updated",
                        {"from": expected_state, "to": state, "observations": observations, "evidence": evidence},
                        trade_id=current["trade_id"], local_order_id=identity if table == "orders" else None)
        return self._one(table, id_column, identity)

    def update_trade_state(self, trade_id, **changes):
        return self._update_state("trades", "trade_id", trade_id, **changes)

    def update_order_state(self, local_order_id, **changes):
        return self._update_state("orders", "local_order_id", local_order_id, **changes)

    def read_bot_state(self):
        return self._one("bot_state", "singleton_id", 1)

    def entry_switch_enabled(self):
        """Local switches only, NOT exchange readiness or permission to submit."""
        state = self.read_bot_state()
        return self._ceiling and bool(state["entries_enabled"]) and not bool(state["critical_latched"])

    def update_bot_state(self, *, run_id, expected_version, updated_at_ms, changes, reason,
                         acknowledge_critical=False):
        _timestamp(updated_at_ms)
        allowed = {"entries_enabled", "critical_latched", "critical_reason", "consecutive_losses", "last_reconciled_at_ms"}
        if not isinstance(changes, Mapping) or not changes or set(changes) - allowed:
            raise ValueError("Unsupported runtime state change")
        if type(expected_version) is not int or type(acknowledge_critical) is not bool:
            raise ValueError("Runtime update requires version and explicit boolean acknowledgement")
        _identifier(reason)
        values = dict(changes)
        for key in ("entries_enabled", "critical_latched"):
            if key in values:
                if type(values[key]) is not bool:
                    raise ValueError("Runtime flags must be boolean")
                values[key] = int(values[key])
        if "consecutive_losses" in values and (type(values["consecutive_losses"]) is not int or values["consecutive_losses"] < 0):
            raise ValueError("Loss count must be a nonnegative integer")
        if values.get("last_reconciled_at_ms") is not None:
            _timestamp(values["last_reconciled_at_ms"])
        if values.get("critical_reason") is not None:
            _identifier(values["critical_reason"])
        with self.transaction():
            current = self.read_bot_state()
            if expected_version != current["version"] or updated_at_ms < current["updated_at_ms"]:
                raise PersistenceConflictError("Stale runtime state update rejected")
            if current["critical_latched"] and values.get("critical_latched") == 0 and not acknowledge_critical:
                raise PersistenceConflictError("Clearing a critical latch requires explicit acknowledgement")
            values.update(version=expected_version + 1, updated_at_ms=_timestamp(updated_at_ms))
            assignments = ",".join(f"{key}=?" for key in values)
            cursor = self._connection.execute(f"UPDATE bot_state SET {assignments} WHERE singleton_id=1 AND version=?",
                                              (*values.values(), expected_version))
            if cursor.rowcount != 1:
                raise PersistenceConflictError("Runtime state was changed by another owner")
            self._audit(run_id, updated_at_ms, "bot_state_updated", {"changes": changes, "reason": reason},
                        severity="CRITICAL" if values.get("critical_latched") else "INFO")
        return self.read_bot_state()

    def get_run(self, run_id):
        return self._one("runs", "run_id", run_id)

    def get_decision(self, decision_id):
        return self._one("decisions", "decision_id", decision_id)

    def get_trade(self, trade_id):
        return self._one("trades", "trade_id", trade_id)

    def get_order(self, local_order_id):
        return self._one("orders", "local_order_id", local_order_id)

    def active_trade(self):
        rows = self._rows("SELECT * FROM trades WHERE state NOT IN ('CLOSED','ABORTED')")
        return rows[0] if rows else None

    def unresolved_orders(self):
        return self._rows("SELECT * FROM orders WHERE state NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED','ABORTED') ORDER BY created_at_ms,local_order_id")

    def signal_consumed(self, signal_key):
        return bool(self._rows("SELECT 1 FROM trades WHERE signal_key=?", (signal_key,)))

    def events(self):
        return self._rows("SELECT * FROM events ORDER BY event_id")
