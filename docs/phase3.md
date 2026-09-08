# Phase 3: durable local persistence

This phase adds an independent SQLite component, not integration with the legacy
bot. No Binance connection, order operation, strategy change, risk sizing change,
or execution/reconciliation engine is included. The operating contract remains
one dedicated USDT-M Futures Testnet account, One-Way mode, BTCUSDT/ETHUSDT/ADAUSDT,
and at most one position across the account. The current 15m timeframe is retained.
No real-money support is introduced.

## Exact schema

[001_initial.sql](../migrations/001_initial.sql) is the authoritative, complete DDL,
including every column, nullability rule, default, index, and trigger.
`PRAGMA user_version` is 1. There are exactly six tables:

| Table | Columns |
| --- | --- |
| runs | run_id, started_at_ms, stopped_at_ms, baseline_commit, config_json, exit_reason |
| decisions | decision_id, run_id, evaluated_at_ms, symbol, timeframe, candle_open_ms, strategy_version, signal, outcome, reason_codes_json, snapshot_json, proposed_plan_json |
| trades | trade_id, decision_id, signal_key, symbol, direction, origin, state, created_at_ms, updated_at_ms, opened_at_ms, closed_at_ms, planned_qty, filled_entry_qty, remaining_qty, avg_entry_price, avg_exit_price, planned_sl_price, planned_tp_price, actual_sl_price, actual_tp_price, approved_risk_usdt, leverage, gross_pnl_usdt, fees_usdt, funding_usdt, net_pnl_usdt, accounting_complete, close_reason |
| orders | local_order_id, trade_id, symbol, role, revision, api_family, client_order_id, exchange_order_id, triggered_order_id, side, position_side, order_type, quantity, trigger_price, working_type, reduce_only, close_position, state, exchange_status, executed_qty, avg_fill_price, created_at_ms, updated_at_ms, request_json, last_response_json |
| bot_state | singleton_id, entries_enabled, critical_latched, critical_reason, consecutive_losses, last_reconciled_at_ms, version, updated_at_ms |
| events | event_id, run_id, occurred_at_ms, severity, event_type, decision_id, trade_id, local_order_id, payload_json |

Timestamps are INTEGER UTC epoch milliseconds. Flags, counters, leverage, revision,
version, singleton_id, and event_id are INTEGER. Other columns are TEXT, including
all quantities, prices, risk amounts, P&L, fees, funding, and JSON documents. Unknown
exchange IDs, execution quantities/prices, protection prices, P&L, and closure times
remain NULL. Zero is not substituted for an unknown outcome.

Trade states: INTENT, ENTRY_PENDING, ENTRY_UNKNOWN, PROTECTING, OPEN_PROTECTED,
EXIT_PENDING, RECONCILIATION_REQUIRED, CLOSED, ABORTED. Origins: BOT, RECOVERED,
EXTERNAL; the current creation API creates BOT intents only.

Order states: INTENT, SUBMITTING, UNKNOWN, NEW, PARTIALLY_FILLED, TRIGGERED, FILLED,
CANCELED, REJECTED, EXPIRED, ABORTED. Roles: ENTRY, SL, TP, EXIT. API families:
STANDARD and ALGO. Position side is constrained to BOTH for One-Way mode.

## Constraints and deduplication

- A partial unique index on the constant expression `(1)` reserves one active
  trade TOTAL. Every state except CLOSED and ABORTED occupies that slot, including
  pending, unknown, and reconciliation-required states.
- `signal_key` uniquely consumes a strategy version/symbol/timeframe/candle-open
  tuple, encoded as a compact JSON array. Direction is deliberately excluded.
  Consumption survives closure, local abort, and restart; no retry with a fresh
  trade for the same signal is permitted.
- Decision IDs can fund only one trade. BOT trades require a decision and signal
  key; the API additionally requires an APPROVED LONG or SHORT decision.
- Client order IDs are globally unique within this account database. Known exchange
  identities are unique by `(symbol, api_family, exchange_order_id)`. Multiple NULL
  identities are permitted before confirmation. `(trade_id, role, revision)` is unique.
- Composite foreign keys prevent mismatched decision/trade/order symbols. All
  references use foreign-key enforcement; JSON columns require valid JSON.
- Unresolved orders prevent releasing a trade slot. New orders cannot be inserted
  against terminal trades. Events reject UPDATE, DELETE, and INSERT OR REPLACE.
- close_position requires quantity NULL and reduce_only false. This is a local
  parameter-shape constraint, not proof that Binance accepts a request.

## Persistence API

`Persistence(path, busy_timeout_ms=5000, trading_enabled_ceiling=False, secrets=())`
initializes/validates storage and acquires ownership. It supports a context manager
and explicit `close()`. It does not import configuration or the Binance client.

| Methods | Responsibility |
| --- | --- |
| validate_schema_version | Validate version, schema manifest, integrity, references, singleton |
| begin_run / end_run | Explicit process history and associated events |
| create_decision | Persist signal, outcome, reasons, snapshot, optional plan |
| create_trade_intent / create_order_intent | Persist local intent with unknown exchange results |
| reserve_entry | Atomically reserve trade and ENTRY order intent, plus events |
| update_trade_state / update_order_state | Expected-state updates with timestamp, evidence, optional observations |
| append_event / events | Append and read audit history |
| read_bot_state / update_bot_state | Read and version-check persistent runtime flags |
| entry_switch_enabled | Combine startup ceiling, stored entry flag, and critical latch |
| get_run / get_decision / get_trade / get_order | Read one record |
| active_trade / unresolved_orders / signal_consumed | Query durable reservations and uncertainty |
| transaction | Compose multiple local operations atomically |

Callers supply IDs and timestamps explicitly. Returned rows are dictionaries;
financial fields remain canonical strings for explicit `decimal_from_text` conversion.
State updates require a nonempty evidence mapping, expected_state, and nondecreasing
updated_at_ms. Known exchange IDs cannot be replaced, and terminal states cannot
reopen. CLOSED requires explicitly recorded zero remaining quantity and closure
time. ABORTED orders must be never-submitted INTENTs (or already ABORTED); unknown
orders cannot be silently abandoned. Aborting a trade after its INTENT state also
requires explicit flat quantity and closure time.

There is deliberately no full exchange transition graph. Caller evidence is recorded,
not independently verified. Future execution/reconciliation must validate that evidence
and confirm flatness from Binance before reporting a successful emergency close.

## Transactions and durability

Connections enable foreign_keys, WAL, busy_timeout (5000 ms default), and synchronous
FULL. Initialization is one explicit migration transaction; no automatic destructive
repair or reset occurs. Existing databases must match version 1 and its schema
manifest and pass quick_check/foreign_key_check with one bot_state row.

Each mutation commits its state change and audit event together. `reserve_entry`
commits the trade reservation, ENTRY intent, and both events together. `transaction()`
uses BEGIN IMMEDIATE with nested savepoints, so callers can additionally compose a
decision and reservation. A failed constraint or event write rolls back the unit.
No future network call may occur while a local transaction is open: commit intent,
submit later, then persist observations in a new transaction.

Constraint/stale-update failures raise critical PersistenceConflictError. Schema
mismatch raises SchemaMismatchError; unavailable/corrupt storage raises
PersistenceUnavailableError. Database failures poison the current store, preventing
continued use until it is reopened and validated. Error messages omit raw SQLite/OS
exception text. Nothing retries a submission, invents an outcome, or rebuilds the DB.

## Runtime ownership and restart

One canonical local database path represents one dedicated account. `runtime.py`
holds a nonblocking OS lock on its `.lock` sidecar for the store lifetime. A second
cooperating process fails with RuntimeOwnershipError. Windows byte locking is tested;
the POSIX flock fallback is not exercised on this Windows host. Locks release on
process death. The sidecar is intentionally retained to avoid lock-file replacement
races and is ignored by Git.

All writers must use this API and the same canonical path on a local filesystem.
This is advisory ownership, not protection against arbitrary SQL clients, separate
database copies, hard-link aliases, network filesystems, or multiple hosts.

New databases initialize entries_enabled=false, critical_latched=false, no last
reconciliation, and version=0. Opening existing storage never resets these values,
clears uncertainty, or marks an unfinished run stopped. Runtime updates require the
current version; clearing an existing critical latch requires explicit acknowledgement
and a reason. A recorded loss count is not an implemented accounting/risk policy.

`entry_switch_enabled()` reports local switches only. It is NOT proof of fresh
reconciliation or authorization to submit an order. It can be true with an old/null
reconciliation timestamp. A future coordinator must additionally require verified
exchange state and Risk Manager approval. The constructor ceiling defaults false;
configuration wiring is deferred. The legacy loop remains unchanged, so
TRADING_ENABLED=False is still declarative there. Risk-reducing actions must remain
independent of entry switches when that coordinator is implemented.

## Decimal and sensitive-data handling

Financial inputs must be finite Decimal instances. Serialization uses fixed notation,
removes insignificant fractional zeros, and canonicalizes negative zero to `0`:
`Decimal('1.2300') -> '1.23'`, `Decimal('1E-8') -> '0.00000001'`.
Serialization does not round to the current Decimal context. Deserialization rejects
noncanonical text. Financial floats are rejected; no SQLite REAL financial columns
exist. API validation supplies canonical formatting; direct SQL is outside that contract.

JSON Decimal values become the same strings. Only run configuration snapshots accept
finite legacy floats, converted using Decimal(str(value)); this does not alter risk
arithmetic. JSON recursively redacts sensitive keys and explicitly registered secret
values. It does not heuristically mask long IDs. Callers must keep credentials and
raw exceptions out of free-text fields; JSON redaction is not blanket sanitization of
every column. No credentials are needed for this phase.

## Future recovery flow (not implemented)

Acquire ownership, validate local storage, record a new run, and keep entries blocked.
Query Binance positions plus standard/algo orders, compare them with persisted intent
and identities, verify SL and TP, and explicitly record reconciliation outcomes.
Unknown submissions require lookup by their existing client IDs before any retry.
Unprotected positions require protection or an emergency close confirmed flat by
Binance. Only after consistency, protection, and risk checks may entry gates permit
new intent. SQLite stores durable local memory; Binance remains live exchange truth.

Before Phase 4, retain the one-account/one-path deployment contract, implement actual
exchange evidence validation, and complete the deferred Testnet SDK/API contract
checks documented in Phase 1. Merely reopening this database does not make the bot
restart-safe for trading.

## Verification and non-goals

The full offline suite contains 98 passing tests: the existing 51 plus 44 persistence
and 3 runtime tests. Coverage includes schema/migration rollback, FK and uniqueness
constraints, cross-symbol reservations, canonical Decimal round trips, state/event
atomicity, stale updates, durable flags/latches, corruption/version failures, exclusive
process ownership, and abrupt exit with committed uncertainty and uncommitted rollback.
Tests use temporary databases and make no Binance calls.

Run: `python -B -m unittest discover -s tests -v`.
Verified with Python 3.12.14 and SQLite 3.53.1 on Windows. No dependencies were added.

No fills, ledger_entries, risk_days, exchange adapter changes, execution engine,
reconciliation, legacy-loop persistence integration, accounting, Docker/VPS, AI,
backtesting, dashboard, or Telegram work is included. No commit is made in Phase 3.
