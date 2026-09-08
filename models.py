"""Shared contracts; strategy continues returning its original strings."""

from dataclasses import dataclass
from enum import Enum
from decimal import Decimal


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    STOPPING = "STOPPING"


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    SL = "SL"
    TP = "TP"
    EXIT = "EXIT"


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    observed_at_ms: int
    symbol: str | None = None  # None means account-wide.

    def __post_init__(self):
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("snapshot_id must be nonempty")
        if type(self.observed_at_ms) is not int or self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be a nonnegative UTC epoch millisecond integer")
        if self.symbol is not None and (not isinstance(self.symbol, str) or not self.symbol.strip()):
            raise ValueError("symbol must be nonempty when supplied")


@dataclass(frozen=True)
class RiskResult:
    approved: bool
    reason_codes: tuple[str, ...]
    snapshot: SnapshotRef

    def __post_init__(self):
        if type(self.approved) is not bool or not isinstance(self.snapshot, SnapshotRef):
            raise ValueError("risk result requires a boolean and snapshot reference")
        if (not isinstance(self.reason_codes, tuple) or not self.reason_codes
                or any(not isinstance(code, str) or not code.strip() for code in self.reason_codes)):
            raise ValueError("risk result requires nonempty reason codes")


# Read observations only; no execution state or authorization is inferred.
@dataclass(frozen=True)
class ExchangeSnapshot:
    source: str
    requested_at_ms: int
    received_at_ms: int


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    status: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal | None
    quantity_precision: int
    price_precision: int
    percent_price_up: Decimal | None
    percent_price_down: Decimal | None


@dataclass(frozen=True)
class ExchangeInfoSnapshot:
    metadata: ExchangeSnapshot
    symbols: tuple[SymbolFilters, ...]


@dataclass(frozen=True)
class BalanceSnapshot:
    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    cross_wallet_balance: Decimal
    cross_unrealized_pnl: Decimal
    update_time_ms: int


@dataclass(frozen=True)
class AccountSnapshot:
    metadata: ExchangeSnapshot
    can_trade: bool
    multi_assets_margin: bool
    wallet_balance: Decimal
    margin_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class BalancesSnapshot:
    metadata: ExchangeSnapshot
    balances: tuple[BalanceSnapshot, ...]


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: Decimal
    position_side: str | None
    entry_price: Decimal | None
    mark_price: Decimal | None
    unrealized_pnl: Decimal | None
    liquidation_price: Decimal | None
    margin_asset: str | None
    update_time_ms: int | None
    inferred_flat: bool = False

    @property
    def direction(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT" if self.quantity < 0 else "FLAT"


@dataclass(frozen=True)
class PositionsSnapshot:
    metadata: ExchangeSnapshot
    positions: tuple[PositionSnapshot, ...]
    issues: tuple[str, ...]

    @property
    def exposure_symbols(self) -> tuple[str, ...]:
        return tuple(sorted({p.symbol for p in self.positions if p.quantity != 0}))

    @property
    def flat(self) -> bool:
        return not self.exposure_symbols and not self.issues


@dataclass(frozen=True)
class OrdinaryOrderSnapshot:
    symbol: str
    order_id: str
    client_order_id: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    executed_quantity: Decimal
    price: Decimal
    stop_price: Decimal | None
    reduce_only: bool
    close_position: bool
    position_side: str
    working_type: str | None


@dataclass(frozen=True)
class AlgoOrderSnapshot:
    symbol: str
    algo_id: str
    client_algo_id: str
    side: str
    order_type: str
    algo_status: str
    trigger_price: Decimal
    quantity: Decimal | None
    reduce_only: bool
    close_position: bool
    position_side: str
    working_type: str
    actual_order_id: str | None
    actual_quantity: Decimal | None


@dataclass(frozen=True)
class OrdinaryOrdersSnapshot:
    metadata: ExchangeSnapshot
    orders: tuple[OrdinaryOrderSnapshot, ...]


@dataclass(frozen=True)
class AlgoOrdersSnapshot:
    metadata: ExchangeSnapshot
    orders: tuple[AlgoOrderSnapshot, ...]


@dataclass(frozen=True)
class FillSnapshot:
    symbol: str
    trade_id: str
    order_id: str
    side: str
    position_side: str
    price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str
    time_ms: int


@dataclass(frozen=True)
class FillsSnapshot:
    metadata: ExchangeSnapshot
    fills: tuple[FillSnapshot, ...]
    limit_reached: bool  # Bounded page, never a claim of complete account history.
