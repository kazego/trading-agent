"""Shared contracts; strategy continues returning its original strings."""

from dataclasses import dataclass
from enum import Enum


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
