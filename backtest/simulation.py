"""Deterministic single-symbol execution. No live state, clients or portfolio logic."""

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from enum import Enum
from functools import wraps

from backtest.clock import HistoricalClock, OpeningEvent
from backtest.data import Candle, DataValidationError, canonical_decimal
from backtest.strategy_adapter import Decision, FrozenStrategyAdapter

D = Decimal
SIMULATION_VERSION = "single-symbol-v1"
EXCLUSIONS = "Net after modeled trading costs only; funding and liquidation excluded. Not complete Futures account returns."


def arithmetic(function):
    """All simulation arithmetic uses a fresh fixed context, independent of callers."""
    @wraps(function)
    def run(*args, **kwargs):
        with localcontext(Context(prec=50)):
            return function(*args, **kwargs)
    return run


def decimal_fields(instance, names):
    for name in names:
        object.__setattr__(instance, name, D(canonical_decimal(getattr(instance, name))))


class CostMode(str, Enum):
    GROSS = "GROSS"
    FEES_ONLY = "FEES_ONLY"
    FEES_AND_SLIPPAGE = "FEES_AND_SLIPPAGE"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    END_OF_DATA = "END_OF_DATA"


class SizingError(DataValidationError):
    """Valid signal cannot produce a permitted simulated quantity."""


@dataclass(frozen=True)
class SimulationConfig:
    starting_equity: Decimal = D("1000")
    risk_fraction: Decimal = D("0.01")
    leverage: Decimal = D("3")
    stop_fraction: Decimal = D("0.015")
    target_fraction: Decimal = D("0.03")
    cost_mode: CostMode = CostMode.GROSS
    entry_fee_rate: Decimal = D("0")
    exit_fee_rate: Decimal = D("0")
    slippage_bps: Decimal = D("0")

    def __post_init__(self):
        decimal_fields(self, ("starting_equity", "risk_fraction", "leverage", "stop_fraction",
                              "target_fraction", "entry_fee_rate", "exit_fee_rate", "slippage_bps"))
        if not isinstance(self.cost_mode, CostMode):
            raise DataValidationError("Explicit CostMode required")
        if self.starting_equity <= 0 or not 0 < self.risk_fraction <= 1 or not 0 < self.leverage <= 3:
            raise DataValidationError("Invalid equity, risk fraction or leverage (maximum 3)")
        if self.stop_fraction != D("0.015") or self.target_fraction != D("0.03"):
            raise DataValidationError("Baseline requires 1.5% SL and 3% TP")
        if not 0 <= self.entry_fee_rate < 1 or not 0 <= self.exit_fee_rate < 1 or not 0 <= self.slippage_bps < 10000:
            raise DataValidationError("Invalid fee/slippage assumption")

    @property
    def entry_rate(self):
        return D(0) if self.cost_mode == CostMode.GROSS else self.entry_fee_rate

    @property
    def exit_rate(self):
        return D(0) if self.cost_mode == CostMode.GROSS else self.exit_fee_rate


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    version: str
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal = D("0")

    def __post_init__(self):
        from backtest.data import SYMBOLS
        decimal_fields(self, ("tick_size", "quantity_step", "min_quantity", "max_quantity", "min_notional"))
        if self.symbol not in SYMBOLS or not isinstance(self.version, str) or not self.version.strip():
            raise DataValidationError("Configured symbol and explicit rule version/assumption required")
        if (self.tick_size <= 0 or self.quantity_step <= 0 or self.min_quantity <= 0
                or self.max_quantity < self.min_quantity or self.min_notional < 0):
            raise DataValidationError("Invalid symbol rules")


@dataclass(frozen=True)
class Fill:
    side: str
    candle_open_time_ms: int
    phase: str  # OPEN, INTRABAR or CLOSE; no invented intrabar execution timestamp.
    reference_price: Decimal
    price: Decimal
    quantity: Decimal
    fee: Decimal

    def __post_init__(self):
        decimal_fields(self, ("reference_price", "price", "quantity", "fee"))
        if (self.side not in ("BUY", "SELL") or self.phase not in ("OPEN", "INTRABAR", "CLOSE")
                or min(self.reference_price, self.price, self.quantity) <= 0 or self.fee < 0):
            raise DataValidationError("Invalid simulated fill")


@dataclass(frozen=True)
class Position:
    decision: Decision
    entry: Fill
    stop_loss: Decimal
    take_profit: Decimal
    equity_before: Decimal
    planned_stop_risk: Decimal

    def __post_init__(self):
        decimal_fields(self, ("stop_loss", "take_profit", "equity_before", "planned_stop_risk"))
        if min(self.stop_loss, self.take_profit, self.equity_before, self.planned_stop_risk) <= 0:
            raise DataValidationError("Invalid simulated position")


@dataclass(frozen=True)
class ClosedTrade:
    position: Position
    exit: Fill
    reason: ExitReason
    dual_touch: bool
    gross_pnl: Decimal
    net_pnl: Decimal

    def __post_init__(self):
        decimal_fields(self, ("gross_pnl", "net_pnl"))
        if not isinstance(self.reason, ExitReason):
            raise DataValidationError("Explicit ExitReason required")


@dataclass(frozen=True)
class Rejection:
    decision_id: str
    candle_open_time_ms: int
    reason: str


@dataclass(frozen=True)
class SimulationResult:
    symbol: str
    config: SimulationConfig
    rules: SymbolRules
    decisions: tuple[Decision, ...]
    trades: tuple[ClosedTrade, ...]
    rejections: tuple[Rejection, ...]
    final_equity: Decimal
    insolvent: bool
    simulation_version: str = SIMULATION_VERSION
    limitations: str = EXCLUSIONS


@arithmetic
def slipped_price(reference, side, config):
    reference = D(canonical_decimal(reference))
    if reference <= 0 or side not in ("BUY", "SELL"):
        raise DataValidationError("Positive reference and BUY/SELL side required")
    slip = config.slippage_bps / D(10000) if config.cost_mode == CostMode.FEES_AND_SLIPPAGE else D(0)
    return reference * (1 + slip if side == "BUY" else 1 - slip)


def _round(value, step, upwards):
    return (value / step).to_integral_value(rounding=ROUND_CEILING if upwards else ROUND_FLOOR) * step


@arithmetic
def protections(entry_price, signal, rules, config):
    entry_price = D(canonical_decimal(entry_price))
    if signal not in ("LONG", "SHORT") or entry_price <= 0:
        raise DataValidationError("Positive entry and LONG/SHORT required")
    long = signal == "LONG"
    stop = _round(entry_price * (1 - config.stop_fraction if long else 1 + config.stop_fraction), rules.tick_size, not long)
    target = _round(entry_price * (1 + config.target_fraction if long else 1 - config.target_fraction), rules.tick_size, long)
    if min(stop, target) <= 0 or not (stop < entry_price < target if long else target < entry_price < stop):
        raise SizingError("Tick rules cannot represent valid protections")
    return stop, target


@arithmetic
def size_quantity(equity, entry_price, stop, signal, rules, config):
    equity, entry_price, stop = (D(canonical_decimal(v)) for v in (equity, entry_price, stop))
    if equity <= 0:
        raise SizingError("Nonpositive simulated equity")
    if entry_price <= 0 or stop <= 0 or signal not in ("LONG", "SHORT"):
        raise DataValidationError("Invalid sizing inputs")
    if not (stop < entry_price if signal == "LONG" else stop > entry_price):
        raise DataValidationError("Stop must be adverse to entry")
    stop_fill = slipped_price(stop, "SELL" if signal == "LONG" else "BUY", config)
    loss_per_unit = abs(entry_price - stop_fill) + entry_price * config.entry_rate + stop_fill * config.exit_rate
    risk_size = equity * config.risk_fraction / loss_per_unit
    # Reserve entry fees in addition to initial margin. Exit costs are in stop risk.
    margin_size = equity / (entry_price / config.leverage + entry_price * config.entry_rate)
    quantity = _round(min(risk_size, margin_size, rules.max_quantity), rules.quantity_step, False)
    if quantity <= 0 or quantity < rules.min_quantity:
        raise SizingError("Risk/margin/quantity caps leave less than minimum quantity")
    if quantity * entry_price < rules.min_notional:
        raise SizingError("Sized notional is below minimum")
    return quantity, quantity * loss_per_unit


@arithmetic
def open_position(decision, event: OpeningEvent, equity, rules, config):
    if (decision.symbol != event.symbol or rules.symbol != event.symbol
            or decision.open_time_ms != event.open_time_ms or decision.timeframe != event.timeframe
            or decision.dataset_fingerprint != event.dataset_fingerprint):
        raise DataValidationError("Decision/event/rule identity mismatch")
    if decision.signal == "NONE":
        return None
    if decision.signal not in ("LONG", "SHORT"):
        raise DataValidationError("Unknown entry signal")
    side = "BUY" if decision.signal == "LONG" else "SELL"
    price = slipped_price(event.opening_price, side, config)
    stop, target = protections(price, decision.signal, rules, config)
    quantity, planned_risk = size_quantity(equity, price, stop, decision.signal, rules, config)
    fill = Fill(side, event.open_time_ms, "OPEN", event.opening_price, price, quantity,
                price * quantity * config.entry_rate)
    return Position(decision, fill, stop, target, D(canonical_decimal(equity)), planned_risk)


@arithmetic
def close_position(position, reference, candle_time, phase, reason, dual_touch, config):
    side = "SELL" if position.decision.signal == "LONG" else "BUY"
    reference = D(canonical_decimal(reference))
    price = slipped_price(reference, side, config)
    quantity = position.entry.quantity
    fill = Fill(side, candle_time, phase, reference, price, quantity, price * quantity * config.exit_rate)
    gross = (price - position.entry.price) * quantity
    if position.decision.signal == "SHORT":
        gross = -gross
    return ClosedTrade(position, fill, reason, dual_touch, gross, gross - position.entry.fee - fill.fee)


@arithmetic
def process_bar(position, candle: Candle, config):
    """Called only AFTER the strategy/open-entry boundary; can now inspect OHLC."""
    if candle.symbol != position.decision.symbol or candle.open_time_ms < position.entry.candle_open_time_ms:
        raise DataValidationError("Exit candle precedes entry or belongs to another symbol")
    long = position.decision.signal == "LONG"
    stop, target = position.stop_loss, position.take_profit
    # Known opening conditions resolve chronology before ambiguous intrabar touches.
    # This also handles an immediately adverse quote after a heavily slipped entry.
    if candle.open <= stop if long else candle.open >= stop:
        return close_position(position, candle.open, candle.open_time_ms, "OPEN", ExitReason.STOP_LOSS, False, config)
    if candle.open >= target if long else candle.open <= target:
        return close_position(position, target, candle.open_time_ms, "OPEN", ExitReason.TAKE_PROFIT, False, config)
    stop_hit = candle.low <= stop if long else candle.high >= stop
    target_hit = candle.high >= target if long else candle.low <= target
    if stop_hit or target_hit:
        return close_position(position, stop if stop_hit else target, candle.open_time_ms, "INTRABAR",
                              ExitReason.STOP_LOSS if stop_hit else ExitReason.TAKE_PROFIT,
                              stop_hit and target_hit, config)
    return None


@arithmetic
def simulate(dataset, symbol, rules, config=SimulationConfig()):
    """One symbol, at most one held position. No arbitration or portfolio metrics."""
    if rules.symbol != symbol:
        raise DataValidationError("Rules must belong to the selected symbol")
    clock = HistoricalClock(dataset, symbol)
    adapter = FrozenStrategyAdapter()
    # Full bars remain local to the simulator and are never passed to the adapter.
    bars = {c.open_time_ms: c for c in dataset.candles if c.symbol == symbol}
    equity = config.starting_equity
    position = None
    decisions, trades, rejections = [], [], []
    for event in clock:
        decision = adapter.evaluate(event)
        decisions.append(decision)
        held_at_open = position is not None
        if not held_at_open and decision.signal != "NONE":
            try:
                position = open_position(decision, event, equity, rules, config)
                equity -= position.entry.fee
            except SizingError as exc:
                rejections.append(Rejection(decision.decision_id, event.open_time_ms, str(exc)))
        candle = bars[event.open_time_ms]  # First full-bar access after decision/entry.
        if position is not None:
            trade = process_bar(position, candle, config)
            if trade is not None:
                trades.append(trade)
                equity += trade.gross_pnl - trade.exit.fee
                position = None
        if equity <= 0:
            break
    if position is not None:
        trade = close_position(position, candle.close, candle.open_time_ms, "CLOSE",
                               ExitReason.END_OF_DATA, False, config)
        trades.append(trade)
        equity += trade.gross_pnl - trade.exit.fee
    return SimulationResult(symbol, config, rules, tuple(decisions), tuple(trades), tuple(rejections), equity, equity <= 0)
