"""Immutable Phase 6B1 policy and identity. No historical data is loaded here."""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re


SYMBOLS = ("BTCUSDT", "ETHUSDT", "ADAUSDT")
DEV_START, VALIDATION_START, HOLDOUT_START = 1704067200000, 1735689600000, 1767225600000
STEP_MS = 900000
RULES_HASH = "e382b5da9ad38a6836e24d488fb8306e61b8849906000ec015d3784486802872"
OBSERVED = "2025 was already observed through baseline-001 and Phase 6A; it is not genuinely unseen validation data."


def canonical(value):
    """Canonical Decimal strings, finite values, string dictionary keys; no floats."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal")
        if value == 0:
            return "0"
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Enum):
        return canonical(value.value)
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("String keys required")
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical(v) for v in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise ValueError("Unsupported value; floats are forbidden")


def encoded(value):
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def fingerprint(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("SHA-256 required")
    return value


def nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Nonempty text required")
    return value


class Role(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "LOCKED_PREVIOUSLY_OBSERVED_VALIDATION"
    HOLDOUT = "TRUE_HOLDOUT"


def policy():
    """Fresh values on every call; callers cannot mutate the approved policy."""
    return dict(version="research-policy-v1", roles={
        Role.DEVELOPMENT.value: [DEV_START, VALIDATION_START],
        Role.VALIDATION.value: [VALIDATION_START, HOLDOUT_START],
        Role.HOLDOUT.value: [HOLDOUT_START, None]}, validation_observation=OBSERVED,
        interval_semantics="[start,end)", starting_equity="1000", start_flat=True,
        carry_equity=False, carry_positions=False, warmup_history_only=True,
        validation_requires_promotion=True, holdout_requires_separate_authorization=True,
        development_budget=3, signal_timeframes=["15m", "1h", "4h"],
        strategy_family="EMA_CROSSOVER", ema_fast=9, ema_slow=21,
        window_completed=99, forming_rows=1, ewm_adjust=False, ema_cache=False,
        symbols=SYMBOLS, priority=SYMBOLS, directions=["LONG", "SHORT"],
        max_positions=1, risk_fraction="0.01", max_leverage="3", sl_fraction="0.015", tp_fraction="0.03",
        execution_resolution="15m", entry="NEXT_SIGNAL_CANDLE_OPEN", dual_touch="STOP_LOSS",
        stop_gap="WORSE_OPEN", tp_gap="NO_FAVORABLE_IMPROVEMENT", end_of_data="FINAL_COMPLETED_CLOSE",
        cost_mode="FEES_AND_SLIPPAGE", entry_fee_rate="0.0005", exit_fee_rate="0.0005", slippage_fraction="0.0002",
        costs_are_research_assumptions=True, rules_version="research-rules-v1", rules_fingerprint=RULES_HASH,
        rules_are_static_not_historical=True, funding=False, liquidation=False,
        eligibility=dict(net_expectancy_gt="0", net_profit_factor_gt="1", active_quarters=4,
            positive_net_quarters_at_least=3, max_mtm_drawdown_pct_lt="25",
            integrity_failure_allowed=False, insolvency_allowed=False, unexplained_reconciliation_error_allowed=False,
            completed_trade_evidence_target=100, trade_target_is_hard_rejection=False,
            smaller_samples="Explicitly label materially smaller samples weaker evidence"))


@dataclass(frozen=True)
class Hypothesis:
    question: str
    mechanism: str
    comparison: str
    parents: tuple[str, ...] = ()
    validation_informed: bool = False

    def __post_init__(self):
        for text in (self.question, self.mechanism, self.comparison):
            nonempty(text)
        object.__setattr__(self, "parents", tuple(self.parents))
        if len(set(self.parents)) != len(self.parents) or type(self.validation_informed) is not bool:
            raise ValueError("Invalid ancestry")
        for parent in self.parents:
            digest(parent)

    @property
    def record(self):
        return canonical(asdict(self))

    @property
    def id(self):
        return fingerprint(self.record)


@dataclass(frozen=True)
class ExperimentSpec:
    signal_timeframe: str
    strategy_version: str
    source_hashes: tuple[tuple[str, str], ...]
    development_dataset_fingerprints: tuple[tuple[str, str], ...]
    software_versions: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if self.signal_timeframe not in ("15m", "1h", "4h"):
            raise ValueError("Outside the three approved concepts")
        nonempty(self.strategy_version)
        for name in ("source_hashes", "development_dataset_fingerprints", "software_versions"):
            entries = tuple(sorted(tuple(pair) for pair in getattr(self, name)))
            if not entries or any(len(pair) != 2 for pair in entries) or len({p[0] for p in entries}) != len(entries):
                raise ValueError("Nonempty unique key/value pairs required")
            for key, value in entries:
                nonempty(key)
                digest(value) if name != "software_versions" else nonempty(value)
            object.__setattr__(self, name, entries)
        if {s for s, _ in self.development_dataset_fingerprints} != set(SYMBOLS):
            raise ValueError("Exactly three development dataset fingerprints required")

    @property
    def record(self):
        return dict(schema_version="experiment-v1", **canonical(asdict(self)), policy=canonical(policy()))

    @property
    def id(self):
        return fingerprint(self.record)


@dataclass(frozen=True)
class SplitRequest:
    role: Role
    start_ms: int
    end_ms: int
    warmup_start_ms: int

    def __post_init__(self):
        if not isinstance(self.role, Role):
            raise ValueError("Explicit Role required; VALIDATION is previously observed")
        for t in (self.start_ms, self.end_ms, self.warmup_start_ms):
            if type(t) is not int or t % STEP_MS:
                raise ValueError("Aligned integer milliseconds required")
        if not 946684800000 <= self.warmup_start_ms <= self.start_ms < self.end_ms < 4102444800000:
            raise ValueError("Invalid interval")
        expected = {Role.DEVELOPMENT: (DEV_START, VALIDATION_START), Role.VALIDATION: (VALIDATION_START, HOLDOUT_START)}
        if self.role in expected and (self.start_ms, self.end_ms) != expected[self.role]:
            raise ValueError("Unauthorized split interval")
        if self.role == Role.HOLDOUT and self.start_ms != HOLDOUT_START:
            raise ValueError("Holdout starts at 2026-01-01")

    @property
    def record(self):
        return canonical(asdict(self))

    def evaluation_allowed(self, open_time_ms):
        return type(open_time_ms) is int and self.start_ms <= open_time_ms < self.end_ms and open_time_ms % STEP_MS == 0
