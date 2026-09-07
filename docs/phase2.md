# Trading Agent v0.2 — Phase 2

Starting point: clean `master` at `18b3ef7564fabd9961ff5929d6259a9e30b0f807`.
This phase introduces validated configuration, small shared types, error
classification, and rotating structured logs. No commit is included.

## Scope and safety boundary

**TRADING_ENABLED is declarative in Phase 2**, as explicitly agreed during
implementation. It defaults to False but does not gate the original loop yet.
Running `bot.py` is not an offline demonstration or a safe disabled-entry mode.
The legacy bot may still place orders if run with credentials. Phase 2 tests
never construct a real Binance client or access Binance.

The fixed BTCUSDT/ETHUSDT/ADAUSDT watchlist, account-wide single-position cap,
One-Way/USDT-M declarations, future risk caps, and timing/retry settings are
validated declarations. Watchlist scheduling, exchange account verification,
limit enforcement, retries, and runtime entry blocking are not implemented.
`SYMBOL` remains a temporary BTCUSDT compatibility alias for the old adapter.

Startup now rejects invalid settings, including non-Testnet mode, before client
construction. Logging initialization also precedes client construction. These
are active checks; they do not establish exchange readiness or safe execution.
No Binance order parameters, strategy behavior, or risk calculations changed.

## Configuration

`config.Settings` is a frozen dataclass. Existing baseline defaults remain at
module level; new defaults are declared in Settings. The module exports every
setting for existing callers. `load_settings()` snapshots those same exported
values, validates them, and copies quantity caps into a read-only mapping.
It does not create directories, open SQLite, read an account, or require keys.
Call validation explicitly: constructing a Settings object alone does not
validate it. Direct `Settings(...)` instances accept overrides for unit tests.

Only `API_KEY` and `API_SECRET` are loaded from the existing environment names
`BINANCE_API_KEY` and `BINANCE_API_SECRET`, defaulting to empty strings. They are
excluded from Settings and its repr. There is no new environment parser or .env
loader. In particular, setting an environment variable named TRADING_ENABLED
does not change the setting. Error messages omit rejected values.

| Setting | Exact default |
| --- | --- |
| USE_TESTNET | True |
| TRADING_ENABLED | False (declarative) |
| SYMBOLS | ("BTCUSDT", "ETHUSDT", "ADAUSDT") |
| SYMBOL | "BTCUSDT" (legacy alias) |
| POSITION_MODE | "ONE_WAY" |
| MARKET_TYPE | "USDT_M" |
| TIMEFRAME | "15m" |
| EMA_FAST / EMA_SLOW | 9 / 21 |
| LEVERAGE / MAX_LEVERAGE | 3 / 3 |
| RISK_PER_TRADE_PCT | 1.0 |
| STOP_LOSS_PCT / TAKE_PROFIT_PCT | 1.5 / 3.0 |
| MAX_OPEN_POSITIONS | 1 across the account (future enforcement) |
| POLL_INTERVAL_SECONDS | 60 |
| MAX_POSITION_NOTIONAL_USDT | Decimal("1000") |
| MAX_POSITION_QTY | BTCUSDT: Decimal("0.01"); ETHUSDT: Decimal("0.1"); ADAUSDT: Decimal("1000") |
| MAX_DAILY_LOSS_PCT | 3.0 |
| MAX_CONSECUTIVE_LOSSES | 3 |
| MAX_DATA_AGE_SECONDS | 120 |
| MANAGEMENT_POLL_SECONDS | 5 |
| API_TIMEOUT_SECONDS | 10 |
| MAX_READ_RETRIES | 3 |
| ENTRY_CONFIRM_TIMEOUT_SECONDS | 15 |
| PROTECTION_TIMEOUT_SECONDS | 10 |
| DATABASE_PATH | PROJECT_ROOT / "data" / "bot.sqlite3" |
| LOG_DIRECTORY | PROJECT_ROOT / "logs" |

PROJECT_ROOT is the directory containing config.py, not the current terminal
directory. Monetary/quantity caps are provisional design defaults, not validated
exchange limits or risk recommendations. Later risk validation must take the
minimum of all applicable caps and each symbol's exchange rules. The existing
float-based risk module and its defaults remain unchanged.

Validation rules:

- USE_TESTNET must be exactly True; TRADING_ENABLED must be an actual boolean.
- Require the fixed ordered watchlist tuple, ONE_WAY, USDT_M, 15m, EMA 9/21,
  and exactly one maximum open position. Reject a divergent legacy SYMBOL.
- Leverage, consecutive-loss count, and durations must be positive integers;
  read retries may be zero. Reject booleans as numbers, numeric strings,
  fractional integer fields, nonfinite and unrepresentably large values.
- Percentages must be greater than zero and below 100. The three existing risk
  percentage fields accept only int/float to preserve legacy arithmetic.
- LEVERAGE <= MAX_LEVERAGE; RISK_PER_TRADE_PCT <= MAX_DAILY_LOSS_PCT.
- Notional and all three symbol quantity caps must be positive finite numbers;
  the mapping must contain exactly the watchlist symbols.
- Entry/protection deadlines and maximum data age must cover one API timeout;
  management polling cannot exceed maximum data age. These are consistency
  checks, not guarantees about later multi-request protection timing.
- Paths must be absolute, without parent traversal; the database name must use
  .db/.sqlite/.sqlite3 and cannot conflict with the log directory. Configuration
  validation is lexical; actual log writability is checked by logging setup.

## Shared types and errors

`models.py` contains string enums Signal (LONG/SHORT/NONE), RuntimeState
(STARTING/RECONCILING/READY/DEGRADED/HALTED/STOPPING), OrderRole (ENTRY/SL/TP/EXIT),
and PositionDirection (LONG/SHORT). There is no runtime state machine.
Strategy still returns ordinary strings; future consumers can use Signal(value).

SnapshotRef identifies an observation using an ID, UTC epoch milliseconds, and
an optional symbol (None means account-wide). RiskResult carries a boolean,
nonempty immutable reason codes, and that reference. Neither calculates risk,
fetches data, approves execution, or proves snapshot freshness.

`errors.py` defines ApplicationError and these classifications:

| Error family | Members | Classification/action |
| --- | --- | --- |
| RecoverableReadError | TransientReadError, ReadTimeoutError, RateLimitError | Recoverable; RETRY_READ |
| CriticalError | ConfigurationError, AuthenticationError, AccountModeError, PersistenceUnavailableError, LoggingUnavailableError | Not recoverable; REQUIRE_OPERATOR |
| CriticalError | UnknownExchangeStateError | Not recoverable; RECONCILE |
| CriticalError | ProtectionFailureError | Not recoverable; PROTECT_POSITION |
| Unknown exception / base ApplicationError | Includes generic TimeoutError and OSError | Not recoverable; REQUIRE_OPERATOR |

All ErrorClassification results declare blocks_entries=True until the condition
is resolved, including stale reads. These flags/actions are metadata only, not
retry or blocking behavior wired into the old loop. RETRY_READ means a future
retry may be considered, not that it is automatically safe to resend an order.
An uncertain submission belongs to UnknownExchangeStateError. Entry blocks must
never block risk-reducing management in later integration.

## Structured logging

`logging_config.configure_logging()` configures only the trading_agent logger.
It writes UTF-8 JSON lines to logs/bot.jsonl by default, and JSON to stderr.
Rotation uses 5 MiB per active file and three backups (bot.jsonl.1 through .3).
It does not modify root logger handlers. Reconfiguration closes/replaces its
handlers to prevent duplicate output. Log setup creates only the log directory;
the proposed database path remains unused. logs/ is already ignored by Git.

Each normal record includes timestamp (UTC with Z), severity, event_type,
message, symbol, decision_id, trade_id, order_id, and run_id. Unavailable context
is null; a configured run ID is inherited. Optional context is recursively
sanitized. Exception logging includes sanitized type/message, not raw traceback
source lines or local variables.

Example synthetic record (single JSON line on disk):

```json
{"timestamp":"2026-09-07T20:00:00.000Z","severity":"WARNING","event_type":"example","message":"API_KEY=[REDACTED]","symbol":"ETHUSDT","decision_id":null,"trade_id":null,"order_id":null,"run_id":"example-run"}
```

Redaction covers explicitly registered credential values, nested sensitive keys,
credential assignments, Bearer/Basic authorization, URL user-info, and long
opaque token-like strings. Both file and console handlers use it. UUID run IDs
remain readable. Unsupported arbitrary objects are omitted instead of invoking
their repr. Heuristic redaction may hide legitimate long identifiers; it is not
a universal detector of every possible unlabelled/encoded secret. Future callers
must use structured sensitive keys, register secrets, and avoid raw request or
environment dumps. This logger does not sanitize third-party/root loggers.

Malformed records produce a minimal logging_format_failure record. Write/rotation
errors emit a minimal logging_write_failure record to stderr, with no raw
message, arguments, path, or exception dump. If stderr also fails, that output
cannot be guaranteed. Initialization failure raises LoggingUnavailableError
without the raw OS error. Runtime logging failures do not yet latch trading;
that behavior requires later runtime integration.

Minimal legacy integration:

- bot.py validates configuration, configures logs with a run ID and redaction,
  records startup/shutdown, and routes its existing log helper through JSON.
- It emits a warning that TRADING_ENABLED is declarative. Legacy signal/trade
  messages use event_type=legacy_bot; structured trade/decision IDs await later
  phases. Existing messages are not proof of confirmed fills or safe closes.
- The raw traceback output is replaced with sanitized exception logging.
  Startup client errors are logged and reraised with a constant safe message.
- binance_client.py changes only leverage-warning logging; SDK calls, parameter
  values, and exception continuation behavior are unchanged.

## Validation and deferred decisions

Run offline from the repository root:

```text
python -B -m unittest discover -s tests -v
```

Tests cover exact defaults, invalid config, redaction, types, classification,
JSON/UTC, real temporary-file rotation, failure fallbacks, initialization order,
and startup/shutdown using an injected fake client. No SDK installation or
credentials are needed. Temporary log files are removed after tests.

Before Phase 3 or later consumers rely on these contracts, review the provisional
quantity/notional/loss/timing defaults, fixed timeframe/watchlist order, and future
entry-switch integration. Margin mode, loss accounting/reset policy, actual
exchange filters/leverage limits, and simultaneous-signal arbitration remain
unresolved. The Phase 1 SDK/Testnet contract checks are still outstanding.

No SQLite persistence, execution engine, reconciliation, retry implementation,
watchlist trading, risk changes, Docker, AI, backtesting, dashboard, Telegram,
VPS work, or dependency installation/upgrade is included in this phase.
