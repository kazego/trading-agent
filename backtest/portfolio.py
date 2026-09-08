"""Synchronized three-symbol scheduling; execution math remains in Phase 5D."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum

from backtest.clock import HistoricalClock
from backtest.data import DataValidationError, STEP_MS, _hash, canonical_decimal
from backtest.simulation import (
    EXCLUSIONS, SIMULATION_VERSION, ClosedTrade, ExitReason, Position, SimulationConfig,
    SizingError, SymbolRules, arithmetic, close_position, open_position, process_bar,
)
from backtest.strategy_adapter import Decision, FrozenStrategyAdapter

PRIORITY = ("BTCUSDT", "ETHUSDT", "ADAUSDT")
PORTFOLIO_VERSION = "portfolio-v1"


class Disposition(str, Enum):
    NONE = "NONE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class DecisionRecord:
    decision: Decision
    disposition: Disposition
    reason: str | None = None


@dataclass(frozen=True)
class BoundaryRecord:
    open_time_ms: int
    equity_before: Decimal
    decisions: tuple[DecisionRecord, ...]
    priority_winner: str | None
    created: Position | None
    closed: ClosedTrade | None
    equity_after: Decimal
    position_symbol_before: str | None
    position_symbol_after: str | None
    state_after: str


def _canonical(value):
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


@dataclass(frozen=True)
class PortfolioResult:
    config: SimulationConfig
    rules: tuple[SymbolRules, ...]
    dataset_fingerprints: tuple[tuple[str, str], ...]
    boundaries: tuple[BoundaryRecord, ...]
    final_equity: Decimal
    terminal_state: str
    priority: tuple[str, ...] = PRIORITY
    portfolio_version: str = PORTFOLIO_VERSION
    execution_version: str = SIMULATION_VERSION
    limitations: str = EXCLUSIONS

    @property
    def trades(self):
        return tuple(record.closed for record in self.boundaries if record.closed is not None)

    @property
    def fingerprint(self):
        return _hash(_canonical(asdict(self)))


def _validate_inputs(datasets, rules):
    for mapping in (datasets, rules):
        if not isinstance(mapping, Mapping) or set(mapping) != set(PRIORITY):
            raise DataValidationError("Portfolio requires exactly BTCUSDT/ETHUSDT/ADAUSDT mappings")
    clocks = {}
    interval = None
    for symbol in PRIORITY:
        if not isinstance(rules[symbol], SymbolRules) or rules[symbol].symbol != symbol:
            raise DataValidationError("Every portfolio symbol requires its own explicit rules")
        metadata = datasets[symbol].provenance
        current = (metadata["start_ms"], metadata["end_ms"], metadata["timeframe"])
        if interval is not None and current != interval:
            raise DataValidationError("Portfolio evaluation boundaries/timeframes must align")
        interval = current
        # Phase 5C validates symbol coverage, full continuity and >=99 warmup.
        clocks[symbol] = HistoricalClock(datasets[symbol], symbol)
    return clocks, interval[1]


@arithmetic
def simulate_portfolio(datasets, rules, config=SimulationConfig()):
    """Realized shared equity; only completed net P&L updates the balance.

    If a higher-priority signal is untradeable, record REJECTED and consider the
    next signal. Once an entry succeeds, lower-priority signals are blocked.
    """
    clocks, end_ms = _validate_inputs(datasets, rules)
    adapter = FrozenStrategyAdapter()
    bars = {symbol: {c.open_time_ms: c for c in datasets[symbol].candles if c.symbol == symbol}
            for symbol in PRIORITY}
    equity = config.starting_equity
    position = None
    records = []
    for synchronized in zip(*(iter(clocks[symbol]) for symbol in PRIORITY), strict=True):
        timestamp = synchronized[0].open_time_ms
        if any(event.open_time_ms != timestamp for event in synchronized):
            raise DataValidationError("Portfolio clocks lost alignment")
        # Obtain ALL opening decisions before accessing this boundary's full bars.
        decisions = tuple(adapter.evaluate(event) for event in synchronized)
        equity_before = equity
        symbol_before = position.decision.symbol if position is not None else None
        held_at_open = position is not None
        closed = created = None
        winner = None
        if held_at_open:
            closed = process_bar(position, bars[symbol_before][timestamp], config)
            if closed is not None:
                equity += closed.net_pnl
                position = None
        dispositions = []
        for event, decision in zip(synchronized, decisions):
            if decision.signal == "NONE":
                dispositions.append(DecisionRecord(decision, Disposition.NONE))
            elif held_at_open:
                dispositions.append(DecisionRecord(decision, Disposition.BLOCKED, "BLOCKED_OPEN_POSITION"))
            elif winner is not None:
                dispositions.append(DecisionRecord(decision, Disposition.BLOCKED, "BLOCKED_PRIORITY"))
            else:
                try:
                    position = open_position(decision, event, equity, rules[event.symbol], config)
                except SizingError as exc:
                    dispositions.append(DecisionRecord(decision, Disposition.REJECTED, str(exc)))
                else:
                    created = position
                    winner = event.symbol
                    dispositions.append(DecisionRecord(decision, Disposition.ACCEPTED))
        # An entry may close immediately, but arbitration is already finished.
        if created is not None:
            closed = process_bar(position, bars[winner][timestamp], config)
            if closed is not None:
                equity += closed.net_pnl
                position = None
        if timestamp == end_ms - STEP_MS and position is not None:
            candle = bars[position.decision.symbol][timestamp]
            closed = close_position(position, candle.close, timestamp, "CLOSE",
                                    ExitReason.END_OF_DATA, False, config)
            equity += closed.net_pnl
            position = None
        symbol_after = position.decision.symbol if position is not None else None
        state = "INSOLVENT" if equity <= 0 else "OPEN" if position is not None else "FLAT"
        records.append(BoundaryRecord(timestamp, equity_before, tuple(dispositions), winner,
                                      created, closed, equity, symbol_before, symbol_after, state))
        if equity <= 0:
            break
    return PortfolioResult(config, tuple(rules[s] for s in PRIORITY),
                           tuple((s, datasets[s].fingerprint) for s in PRIORITY), tuple(records),
                           equity, "INSOLVENT" if equity <= 0 else "COMPLETED")
