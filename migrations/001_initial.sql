-- One local database represents one dedicated v0.2 Testnet account.
BEGIN IMMEDIATE;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0),
    stopped_at_ms INTEGER CHECK (stopped_at_ms >= started_at_ms),
    baseline_commit TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK (json_valid(config_json)),
    exit_reason TEXT
);

CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    evaluated_at_ms INTEGER NOT NULL CHECK (evaluated_at_ms >= 0),
    symbol TEXT NOT NULL CHECK (symbol IN ('BTCUSDT','ETHUSDT','ADAUSDT')),
    timeframe TEXT NOT NULL CHECK (timeframe = '15m'),
    candle_open_ms INTEGER CHECK (candle_open_ms >= 0),
    strategy_version TEXT NOT NULL,
    signal TEXT NOT NULL CHECK (signal IN ('LONG','SHORT','NONE')),
    outcome TEXT NOT NULL CHECK (outcome IN ('NO_SIGNAL','BLOCKED','APPROVED','ERROR','MANAGEMENT')),
    reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    proposed_plan_json TEXT CHECK (proposed_plan_json IS NULL OR json_valid(proposed_plan_json)),
    UNIQUE (decision_id, symbol)
);

CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY NOT NULL,
    decision_id TEXT UNIQUE,
    signal_key TEXT UNIQUE,
    symbol TEXT NOT NULL CHECK (symbol IN ('BTCUSDT','ETHUSDT','ADAUSDT')),
    direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    origin TEXT NOT NULL CHECK (origin IN ('BOT','RECOVERED','EXTERNAL')),
    state TEXT NOT NULL CHECK (state IN ('INTENT','ENTRY_PENDING','ENTRY_UNKNOWN','PROTECTING',
        'OPEN_PROTECTED','EXIT_PENDING','RECONCILIATION_REQUIRED','CLOSED','ABORTED')),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    opened_at_ms INTEGER CHECK (opened_at_ms >= 0),
    closed_at_ms INTEGER CHECK (closed_at_ms >= 0),
    planned_qty TEXT,
    filled_entry_qty TEXT,
    remaining_qty TEXT,
    avg_entry_price TEXT,
    avg_exit_price TEXT,
    planned_sl_price TEXT,
    planned_tp_price TEXT,
    actual_sl_price TEXT,
    actual_tp_price TEXT,
    approved_risk_usdt TEXT,
    leverage INTEGER CHECK (leverage > 0),
    gross_pnl_usdt TEXT,
    fees_usdt TEXT,
    funding_usdt TEXT,
    net_pnl_usdt TEXT,
    accounting_complete INTEGER NOT NULL DEFAULT 0 CHECK (accounting_complete IN (0,1)),
    close_reason TEXT,
    FOREIGN KEY (decision_id, symbol) REFERENCES decisions(decision_id, symbol),
    CHECK (origin != 'BOT' OR (decision_id IS NOT NULL AND signal_key IS NOT NULL)),
    UNIQUE (trade_id, symbol)
);

-- Constant expression: one slot TOTAL, including pending/unknown reservations.
CREATE UNIQUE INDEX one_active_trade_total ON trades((1))
    WHERE state NOT IN ('CLOSED','ABORTED');

CREATE TABLE orders (
    local_order_id TEXT PRIMARY KEY NOT NULL,
    trade_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ENTRY','SL','TP','EXIT')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    api_family TEXT NOT NULL CHECK (api_family IN ('STANDARD','ALGO')),
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    triggered_order_id TEXT,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    position_side TEXT NOT NULL CHECK (position_side = 'BOTH'),
    order_type TEXT NOT NULL,
    quantity TEXT,
    trigger_price TEXT,
    working_type TEXT CHECK (working_type IN ('MARK_PRICE','CONTRACT_PRICE')),
    reduce_only INTEGER NOT NULL CHECK (reduce_only IN (0,1)),
    close_position INTEGER NOT NULL CHECK (close_position IN (0,1)),
    state TEXT NOT NULL CHECK (state IN ('INTENT','SUBMITTING','UNKNOWN','NEW','PARTIALLY_FILLED',
        'TRIGGERED','FILLED','CANCELED','REJECTED','EXPIRED','ABORTED')),
    exchange_status TEXT,
    executed_qty TEXT,
    avg_fill_price TEXT,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    last_response_json TEXT CHECK (last_response_json IS NULL OR json_valid(last_response_json)),
    FOREIGN KEY (trade_id, symbol) REFERENCES trades(trade_id, symbol),
    UNIQUE (symbol, api_family, exchange_order_id),
    UNIQUE (trade_id, role, revision),
    CHECK (close_position = 0 OR (quantity IS NULL AND reduce_only = 0))
);

CREATE TABLE bot_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    entries_enabled INTEGER NOT NULL CHECK (entries_enabled IN (0,1)),
    critical_latched INTEGER NOT NULL CHECK (critical_latched IN (0,1)),
    critical_reason TEXT,
    consecutive_losses INTEGER NOT NULL CHECK (consecutive_losses >= 0),
    last_reconciled_at_ms INTEGER CHECK (last_reconciled_at_ms >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    CHECK (critical_latched = 0 OR length(trim(critical_reason)) > 0),
    CHECK (critical_latched = 0 OR critical_reason IS NOT NULL)
);

INSERT INTO bot_state VALUES (1, 0, 0, NULL, 0, NULL, 0, 0);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY CHECK (event_id > 0),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
    severity TEXT NOT NULL CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')),
    event_type TEXT NOT NULL,
    decision_id TEXT REFERENCES decisions(decision_id),
    trade_id TEXT REFERENCES trades(trade_id),
    local_order_id TEXT REFERENCES orders(local_order_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE INDEX decisions_by_time ON decisions(evaluated_at_ms);
CREATE INDEX orders_by_state ON orders(state);
CREATE INDEX events_by_trade ON events(trade_id, occurred_at_ms);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'Events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Events are append-only'); END;
CREATE TRIGGER events_no_replace BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'Events are append-only'); END;

CREATE TRIGGER prevent_trade_release_with_unresolved_orders BEFORE UPDATE OF state ON trades
WHEN NEW.state IN ('CLOSED','ABORTED') AND EXISTS (
    SELECT 1 FROM orders WHERE trade_id = NEW.trade_id
    AND state NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED','ABORTED'))
BEGIN SELECT RAISE(ABORT, 'Unresolved orders still reserve the slot'); END;

CREATE TRIGGER prevent_order_on_terminal_trade BEFORE INSERT ON orders
WHEN EXISTS (SELECT 1 FROM trades WHERE trade_id = NEW.trade_id AND state IN ('CLOSED','ABORTED'))
BEGIN SELECT RAISE(ABORT, 'Trade is terminal'); END;

PRAGMA user_version = 1;
COMMIT;
