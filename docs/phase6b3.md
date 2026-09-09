# Phase 6B3: typed EMA adapter and narrow scheduler seam

This phase adds no experiment runner, data acquisition, scorecard or financial
comparison. No saved research dataset is evaluated during implementation/tests.
Tests use synthetic opening windows, synthetic development data views and short
synthetic portfolio fixtures. Baseline-001 is not rerun or rewritten.

## Strategy contract

`research.strategy.Strategy` is the narrow typed protocol:
`evaluate(OpeningWindow) -> SignalDecision`. Only `EMACrossover` is implemented.
The frozen `EMAConfig` validates integer spans with `1 <= fast < slow`.
`ResearchSchedule` currently accepts only explicitly selected EMA 9/21, as
approved by Phase 6B1. No other span combinations are registered or evaluated.
The adapter/config are immutable after construction; there is no indicator cache.

OpeningWindow carries symbol, signal timeframe, opening/completion timestamps,
99 immutable completed aggregate candles, opening price and visible fingerprint.
It contains no full dataset, filesystem path, future component/aggregate candles
or execution results. The normal issuance path is `ResearchSchedule.window_at`
from a validated Phase 6B2 SignalDataView. An internal issuer marker rejects
accidentally constructed raw windows. Explicit synthetic test helpers issue
windows privately; this is not an adversarial Python introspection sandbox.

The adapter reconstructs a fresh 100-row float64 pandas frame: 99 completed
signal candles plus a synthetic forming row with OHLC equal to the current open
and volume zero. Decimal remains in data models and converts only at this
boundary. Nonfinite, overflowing or underflowed financial values fail closed.

EMA uses `ewm(adjust=False)`, independently recomputed for every rolling frame.
The crossover compares -3 against -2, with <= then > for LONG, >= then < for
SHORT, otherwise NONE. Thus -3 is i-2 and -2 is i-1 at signal opening i.
Short visible history is rejected, matching the frozen baseline adapter. The
typed EMA implementation also retains the mathematical insufficient-EMA-history
NONE branch, though approved 9/21 with 100 rows does not need that branch.

SignalDecision records LONG/SHORT/NONE; family/version and typed parameters;
symbol and signal timeframe; decision timestamp; completed timestamps; visible
fingerprint; reason (CROSS_ABOVE, CROSS_BELOW, NO_CROSS or insufficient history);
source hash; Python/pandas/numpy versions; and a deterministic decision ID.
Source hashing normalizes checkout newlines. No runtime/retrieval timestamp or
local path enters identity. Identical visible input and implementation produce
identical decisions. A changed current open may change identity but cannot alter
the completed-candle EMA crossover; the already-issued frozen decision is intact.

## Authorized boundary schedule

`ResearchSchedule(views, explicitly_selected_strategy)` checks Phase 6B2 view
type, Phase 6B1 policy fingerprint and exact DEVELOPMENT interval, full chronology,
99-candle warmup, aggregate content and provenance fingerprints. It copies the
view collection into a read-only mapping. All supplied symbols use one signal
timeframe. Arbitrary raw candle lists, altered fingerprints and invalid split
roles are rejected. No filesystem loading API is provided by the schedule.

`evaluate(symbol, timestamp)` calls the adapter only on signal openings:

| Signal timeframe | Authorized decision openings | Execution cadence |
| --- | --- | --- |
| 15m | Every UTC quarter hour | 15m |
| 1h | Every UTC hour | 15m |
| 4h | 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC | 15m |

At other valid 15m boundaries it returns Python `None`, not a fabricated NONE
signal. Before/after the development interval and at warmup timestamps it rejects
access. 2025 validation and 2026 holdout remain inaccessible.

For 4h 08:00, 12:00 and 16:00 openings, the latest completed buckets start at
04:00, 08:00 and 12:00 respectively. Tests change later 15m H/L/C/V within the
current bucket and the next bucket's opening, rebuild the aggregate view, and
verify an identical earlier SignalDecision. Full dataset/view provenance can
change without changing that visible decision identity.

## Portfolio integration

`backtest.portfolio.simulate_portfolio` adds one keyword-only argument:
`decision_provider: Callable[[OpeningEvent], Decision | None] | None`.

When omitted, the default still constructs FrozenStrategyAdapter and preserves
Phase 5E scheduling, priority, position limits and financial behavior. The engine
does not import/select a research strategy. An explicitly supplied provider is
called on the existing 15m opening events before current full bars are accessed.
Supplied decision symbol, execution timeframe, timestamp, source dataset and
signal are checked before financial processing.

ResearchSchedule is callable as that provider. It checks the source fingerprint
against the view, obtains only the authorized opening window, checks the opening
quote, and wraps its SignalDecision in an immutable ExecutionDecision subclass
of the existing Decision. The envelope explicitly distinguishes:

- `timeframe = 15m`: execution resolution expected by Phase 5D/5E;
- `signal_timeframe = 15m/1h/4h`: strategy resolution;
- original `signal_decision_id`, family/reason and EMA parameters;
- source execution dataset fingerprint and envelope decision ID.

The envelope fingerprint includes the source dataset, so whole-dataset changes
may change it even when the underlying visible SignalDecision is unchanged.
No sizing, fees, slippage, fills, protections or P&L calculations are duplicated.

Provider `None` means no evaluation record at that boundary; genuine NONE signals
are still recorded. Existing position management and end-of-data closure continue
on every 15m boundary regardless. The default continues to produce its original
three decisions per boundary. Research-aware comparison/report presentation is
deferred to Phase 6B4; no report or runner is introduced here.

## Verification

Synthetic 15m tests prove signal equivalence to FrozenStrategyAdapter for LONG,
SHORT, NONE, equality crossings and repeated rolling evaluations; insufficient
visible history is rejected by both. Tests cover raw-window/policy denial,
float64 frame construction, forming isolation, future-component isolation,
deterministic identity, 1h/4h schedules, and immutable inputs.

Short synthetic scheduler tests compare the explicit frozen provider and default
path for identical full PortfolioResult objects/fingerprints, assert that no
baseline adapter is selected for an explicit provider, and confirm stop handling
continues on boundaries without decisions. Existing baseline tests remain intact.

Only synthetic financial fixtures execute in tests. No Phase 6C development
experiment, validation evaluation, 2026 data access or Phase 6B4 work occurs.
No dependencies or strategy.py changes are needed. Actual future experiment
execution must still integrate registry registration/access records and its
explicit authorization in a later runner; this seam does not provide a runner.
