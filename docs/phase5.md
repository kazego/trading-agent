# Phase 5B: offline historical data layer

Phase 5A assumptions remain approved: 1,000 USDT equity, 1% risk, 3x leverage,
one portfolio position, BTCUSDT/ETHUSDT/ADAUSDT priority, next-open entry,
entry-candle protections, SL-first ambiguity, adverse stop gaps, no favorable TP
gap improvement, and END_OF_DATA closure. Fees/slippage will be configurable.
Funding and liquidation are excluded from baseline v1 and future results must say
so. None of these execution/risk/metric behaviors is implemented in Phase 5B.
The existing EMA 9/21 strategy and risk code are unchanged.

## Local ingestion

`backtest.data.ingest` accepts explicit `Source` objects, each naming a local CSV
or ZIP, its symbol, a stable nonempty source identity, and an optional SHA-256
checksum. No filesystem scanning, downloader, API client, credentials, or live
application imports are involved. ZIPs must contain exactly one CSV; content is
read in memory without extracting paths. Source identity is an operator assertion:
OHLC rows alone cannot prove the venue or symbol. Supply genuine USD-M sources
for future historical studies; the checked-in fixtures are entirely synthetic.

Supported archive rows have twelve Binance kline fields in this order:
open time, open, high, low, close, volume, close time, quote volume, count,
taker-buy volume, taker-buy quote volume, ignore. Headerless files and the exact
`HEADER` in `backtest/data.py` are accepted, including UTF-8 BOM. The last five
fields are not retained or validated financially because this layer does not use
them. Unknown headers, wrong field counts, empty files and invalid encoding fail.

Example using ONLY the checked-in synthetic file:

```python
from pathlib import Path
from backtest.data import Source, ingest

dataset = ingest(
    [Source(Path("tests/fixtures/historical/valid.csv"), "BTCUSDT", "synthetic-v1")],
    symbols=("BTCUSDT",), timeframe="15m", timestamp_unit="ms",
    start_ms=1704067200000, end_ms=1704069900000,
    as_of_ms=1704069900000, warmup_candles=0,
)
# Optional explicit creation of a NEW historical artifact:
# dataset.write("historical-dataset.json")
```

## Schema and storage

Each immutable Candle contains `symbol`, `timeframe`, integer `open_time_ms` and
`close_time_ms`, and Decimal `open`, `high`, `low`, `close`, `volume`.
The logical key is `(symbol, timeframe, open_time_ms)`. Financial floats and
non-finite values are rejected. Canonical export uses fixed-point decimal strings,
without redundant trailing fractional zeros or signed zero. Normalization does
not round using the ambient Decimal context.

Phase 5B uses an immutable in-memory dataset and an optional canonical JSON
artifact (candles plus provenance), rather than adding a historical SQLite
database now. This is the minimal storage variation from Phase 5A's SQLite
recommendation. It has no dependency on live persistence or runtime state.
`Dataset.write` requires `.json` and exclusive creation: it cannot overwrite an
existing file. No files are written by ingestion or import. There is no artifact
reload API yet; source re-ingestion reconstructs the validated Dataset. Consider
indexed historical SQLite storage only if dataset sizes require it, separately
from the live database. Current parsing is in memory, not a streaming solution.

## Validation and selection

- Only BTCUSDT, ETHUSDT and ADAUSDT, and only 15m, are allowed.
- The caller must explicitly specify `timestamp_unit="ms"`. Epoch values must
  lie in 2000..2100; seconds, microseconds and nanoseconds are not auto-converted.
- UTC opens align to 900,000 ms. Close time is open + 899,999.
- OHLC prices are finite and positive, volume finite and nonnegative, and
  low <= min(open, close) <= max(open, close) <= high.
- Rows within each source are nondecreasing by open time. Distinct candles must
  be exactly 900,000 ms apart. Files can arrive in any order and can overlap.
- Financially identical normalized duplicate candles collapse deterministically;
  the duplicate count records extra occurrences across all sources. Differences
  in unused archive columns do not constitute conflicting candle values.
  Conflicting modeled candle values fail, even outside the requested interval.
- All supplied data is validated before selection. A gap anywhere in a supplied
  symbol series fails; there is no interpolation, silent sorting of source rows,
  skipping a missing symbol, or synthetic price padding.
- Evaluation uses aligned, nonempty `[start_ms,end_ms)` open times. Requested
  warmup adds the immediately preceding N candles for EVERY configured symbol.
  Missing warmup fails. Dataset output includes warmup; provenance distinguishes
  it from the evaluation interval. No strategy warmup default is imposed here.
- A candle is complete only when `close_time_ms < as_of_ms`. Incomplete/future
  unique candles are excluded and counted. If requested evaluation/warmup needs
  one of those candles, the entire request fails. The explicit cutoff is logical
  dataset input, never the current wall clock.
- Output is ordered by open time, then fixed BTC/ETH/ADA symbol order.

## Reproducibility

Provenance stores source identities, symbols, actual raw-file SHA-256 hashes,
optional supplied checksums, ZIP member names, schema/normalization versions,
symbols/timeframe/unit, interval, completion cutoff, warmup, duplicate count and
excluded incomplete count. Supplied checksums must match actual file bytes;
pass the 64-character digest from a companion checksum file explicitly.

`normalized_candle_fingerprint` is SHA-256 of canonical selected candle records.
`dataset_fingerprint` also covers all canonical provenance, including that candle
fingerprint. Source list ordering and local filesystem paths do not affect hashes.
Source byte changes do affect provenance even if normalized candles are equal.
Changing cutoff or interval is intentionally a different dataset contract. No
wall-clock timestamps enter either fingerprint. Hashes provide identity/integrity,
not proof that data is authentic Binance data or historically correct.

## Fixtures and checks

`tests/fixtures/historical` contains tiny synthetic archive-format files for valid
contiguity, invalid OHLC, microsecond timestamps, gaps, identical/conflicting
duplicates, incomplete tails and insufficient warmup. `multi_symbol.json` maps
the valid synthetic series to each configured symbol for a portfolio data test.
All times start at 2024-01-01 00:00 UTC. Tests supply cutoffs explicitly; incomplete
and insufficient-warmup classifications are request-dependent.

Run the complete offline suite with `python -m unittest discover -s tests -v`.
No test downloads historical data or performs a Binance call. Existing mocked
startup tests still exercise legacy bot import under their fake client; do not
invoke the actual bot. `bot.py` remains incompatible and must not be run.
Live Demo contract validation is still deferred for this operator. No Phase 5C
simulation clock, strategy invocation, order execution, metrics or live state
integration is included.

## Phase 5C: historical clock and frozen strategy adapter

Phase 5B canonical JSON storage and the lack of artifact reload are approved
baseline decisions. Phase 5C consumes validated in-memory datasets directly.

`HistoricalClock(dataset, symbol)` in `backtest/clock.py` iterates one symbol
independently. It requires at least 99 requested warmup candles and verifies
contiguous complete coverage. It emits only the evaluation interval's opens:
`start_ms <= open_time_ms < end_ms`. Extra warmup is allowed, but every event
contains exactly the latest 99 completed candles. Re-iteration restarts replay;
there is no wall-clock dependency, cross-symbol priority or arbitration.

Each immutable `OpeningEvent` exposes the symbol, timeframe, current open time,
opening price, `completed_through_ms = open_time_ms - 1`, a tuple of 99 immutable
completed candles and the dataset fingerprint. It does not expose the current
bar's eventual high/low/close/volume, the next open, or a Dataset reference. The
clock necessarily holds the historical source internally; the event interface
is a look-ahead prevention boundary, not a Python security sandbox. Consumers
must use events rather than access clock internals or the original dataset.

`FrozenStrategyAdapter().evaluate(event)` in `backtest/strategy_adapter.py`
constructs a fresh DataFrame and calls the existing `strategy.get_signal` once:

- Rows 0..98 are completed candles i-99..i-1, oldest first.
- Row 99 is synthetic forming candle i. Its OHLC equals its known opening price;
  volume is zero. Its scheduled close timestamp is calendar information, not
  a claim that this candle is complete.
- Thus `-3` is i-2, `-2` is i-1, and `-1` is the synthetic forming row.
- Historical financial values remain Decimal until conversion into explicit
  pandas float64 columns. Overflow, non-finite values and nonzero values that
  underflow to zero fail closed. Ordinary float64 rounding intentionally matches
  the frozen pandas strategy; EMA arithmetic is not replaced with Decimal.
- EMA configuration must remain 9/21. The adapter checks without changing config.
  It never changes `strategy.py`, including its `ewm(adjust=False)`, equality,
  LONG/SHORT/NONE or minimum-23-row behavior. The stricter clock refuses fewer
  than 99 warmup candles instead of invoking that short-history branch.
- There is no full-history EMA cache. Recomputing within each 100-row window is
  required: a different EMA initialization history can change crossover signals.

The immutable `Decision` records symbol/timeframe, opening decision time,
completed boundary, signal, all 99 completed candle open-time references,
dataset fingerprint and visible-window fingerprint. It also records strategy
and adapter versions, newline-normalized strategy source SHA-256, EMA spans,
pandas/NumPy/Python versions and a deterministic content-derived decision ID.
Dataset fingerprint plus candle references identify the exact historical input.
No random IDs, wall-clock timestamps or trade intents are generated.

Changing future data changes the full dataset fingerprint and therefore the
decision ID, by design. It must NOT change the earlier signal or visible-window
fingerprint. This distinction preserves complete provenance without using future
prices for decisions. Identical inputs and software versions reproduce the full
Decision exactly. A future change to adapter behavior must bump its version.

Example using an already ingested dataset with `warmup_candles=99` (or more):

```python
from backtest.clock import HistoricalClock
from backtest.strategy_adapter import FrozenStrategyAdapter

adapter = FrozenStrategyAdapter()
for event in HistoricalClock(dataset, "BTCUSDT"):
    decision = adapter.evaluate(event)
    # Signal-only result; no trade or account action.
```

Offline tests cover deterministic replay, half-open boundaries, extra/missing
warmup, exact repeated windows, LONG/SHORT/NONE/equality equivalence, edits to
future candles and current eventual HLCV, next-open visibility, per-symbol
independence, configuration rejection, float conversion bounds and immutable
inputs. All Phase 1 tests remain unchanged. Existing full-suite startup tests
still import and exercise the legacy bot under a fake client; the Phase 5C
modules/tests never import it or connect to Binance.

Phase 5D remains unimplemented: entries/fills, positions, protection processing,
sizing, leverage, fees and slippage. Portfolio scheduling, metrics, downloads and
live execution are also absent. `bot.py` must not be used as a trading entry point.

## Phase 5D: single-symbol simulated execution

`backtest/simulation.py` adds pure Decimal execution policies and
`simulate(dataset, symbol, rules, config)`. This runs one selected symbol with
at most one held position. There is no multi-symbol selection or portfolio
arbitration. It consumes the same validated dataset and Phase 5C clock/adapter.
No live persistence, Binance adapter, credentials or bot trading entry point is
used. No historical data is downloaded. The examples and tests use synthetic data.

### Models and inputs

Frozen dataclasses describe SimulationConfig, SymbolRules, Fill, Position,
ClosedTrade, Rejection and SimulationResult. Fill prices, quantity, fees, equity,
thresholds and P&L use Decimal. Financial input floats are rejected. Arithmetic
uses a fresh precision-50 Decimal context (ROUND_HALF_EVEN for ordinary arithmetic)
so caller context cannot change results. Grid rounding is explicitly directional.

Default starting equity is 1,000 USDT, risk fraction 0.01 and leverage 3. Equity
must be positive, risk fraction must be in (0,1], and leverage in (0,3]. Changing
risk/leverage is an explicit experiment input, never a Phase 2 live setting.
The baseline SL/TP fractions remain fixed at 0.015/0.03. The result retains the
entire immutable config and explicit symbol rules, plus a simulation version.

SymbolRules require symbol, nonempty rule version/assumption label, positive
tick/quantity step/minimum quantity, maximum quantity >= minimum, and optionally
a nonnegative minimum notional. Rules are supplied locally; the simulator makes
no claim that they are historically accurate exchange filters. Quantity uses a
zero-origin lot grid (multiples of step), then min/max bounds. No automatic
symbol rules, provisional live caps or leverage changes exist.

### Decision and entry boundary

For each opening event the adapter first computes a decision. If flat at the
boundary and LONG/SHORT, sizing and an opening fill follow, using ONLY the event's
opening price. NONE produces no entry. A position already held at the boundary
prevents a new entry there, including when that position subsequently closes on
an opening gap. There is no re-entry within a candle. Signals are still evaluated
and retained while a position is held; no order intent is generated.

Only after decision/entry does `process_bar` access the current candle's full
OHLC. It is never supplied to the strategy adapter. Thus changing subsequent
market behavior can change a trade outcome without changing its opening signal
or visible-window fingerprint. Whole-dataset provenance IDs can still change,
as accepted in Phase 5C.

### Costs, thresholds and sizing

CostMode explicitly selects GROSS (fees/slippage disabled), FEES_ONLY (configured
entry/exit fees, no slippage), or FEES_AND_SLIPPAGE (both fees and adverse bps).
Fee defaults are zero; operators must supply their documented assumptions for
nonzero-cost studies. No rate is represented as Binance's current account rate.
Inactive cost parameters remain in config but do not affect fills.

With s = slippage_bps / 10,000, BUY fills use reference*(1+s), SELL fills use
reference*(1-s). Require 0 <= bps < 10,000. This applies to entries, SL, TP and
end-of-data exits. Fill prices retain their Decimal modeled value; there is no
additional tick rounding of slipped fill prices. Fees are fill price*quantity*rate,
with rates in [0,1). No fee rounding or fee-asset conversion is assumed.

Let P be the actual slipped entry fill. LONG stop/target are P*0.985/P*1.03;
SHORT stop/target are P*1.015/P*0.97. Round thresholds AWAY from entry: LONG stop
down and target up; SHORT stop up and target down. Reject nonpositive or invalid
thresholds after rounding.

Let S be the rounded stop, Sfill its adverse exit fill, E current equity, r risk
fraction, L leverage, and fe/fx effective entry/exit fee rates:

```text
loss_per_unit = abs(P - Sfill) + P*fe + Sfill*fx
risk_quantity = E*r / loss_per_unit
margin_quantity = E / (P/L + P*fe)
quantity = floor_to_step(min(risk_quantity, margin_quantity, max_quantity))
planned_stop_risk = quantity * loss_per_unit
```

Reject zero, below-minimum quantity or below-minimum notional explicitly as a
Rejection linked to its decision. No automatic bump to a minimum is allowed.
The margin cap reserves the entry fee as well as initial margin. Invalid config,
rules, identities or malformed state fail the run; only untradeable sizing is a
recorded rejection. Gap losses may exceed planned risk. Example: E=1,000, r=1%,
P=100, S=98.5, no costs, step=.001 and loose caps yield quantity=6.666 and planned
stop risk=9.999.

### Protection and closing

Protections apply immediately, including the entry candle. Threshold equality
triggers. Opening conditions are considered first: adverse opening beyond SL
uses that worse open; favorable opening beyond TP uses the target with no price
improvement. Then apply adverse exit slippage. Opening precedence also applies
when a very heavily slipped entry leaves the opening quote already beyond its
new stop: the immediate stop uses the adverse quote, not a better stop reference.

If the open does not determine an exit, LONG checks low<=SL/high>=TP and SHORT
checks high>=SL/low<=TP. If both touch, STOP_LOSS wins and `dual_touch=True`.
Opening-resolved exits are not marked ambiguous, even if the full range touches
both levels. OHLC cannot establish exact intrabar execution timestamps: fills
record candle open time and phase OPEN/INTRABAR/CLOSE, without inventing a time.

Any position remaining after the final completed candle exits at its close with
configured exit costs and reason END_OF_DATA. SL/TP already triggered on that
candle takes precedence. Gross P&L is (exit-entry)*quantity for LONG and its
negative for SHORT. Net subtracts entry and exit fees. Entry fees debit equity
once at entry; exit adds gross less exit fee. Closed equity therefore equals
starting equity plus closed net results. Equity <= 0 after an exit stops replay,
marks insolvency and preserves the actual negative balance; losses are not clipped.

Results contain trades, decisions, rejections and ending equity, without aggregate
performance metrics. They explicitly state: **Net after modeled trading costs
only; funding and liquidation excluded. Not complete Futures account returns.**
This is an OHLC simulation, not a liquidation/margin-call or exchange queue model.

### Tests and next boundary

Offline manual checks cover mirrored LONG/SHORT stop and target P&L, exact touches,
dual-touch, stop/target gaps, adverse fills on all exits, the 2.8985 fee example,
6.666 sizing, cost-inclusive stop risk, fee-reserved margin, lot/min/max/notional
rules, invalid inputs, entry-candle exits, no re-entry, END_OF_DATA, insolvency,
deterministic replay across Decimal contexts and post-decision outcome changes.
Existing Phase 1/5B/5C tests remain unchanged. As before, existing full-suite
startup tests run only under their fake client, not as a bot trading entry point.

Phase 5E remains unimplemented: shared equity and one position TOTAL across the
three symbols, synchronized portfolio scheduling and approved BTC/ETH/ADA signal
priority. No simultaneous-signal arbitration, aggregate metrics, Strategy Lab,
downloader or live execution is introduced here.

## Phase 5E: synchronized portfolio scheduling

`backtest/portfolio.py` adds `simulate_portfolio(datasets, rules, config)`.
Inputs are mappings with exactly BTCUSDT, ETHUSDT and ADAUSDT keys. Each value
is a validated in-memory Dataset or that symbol's explicit SymbolRules. The
scheduler uses a fixed BTCUSDT -> ETHUSDT -> ADAUSDT tuple; caller mapping
insertion order never determines evaluation, arbitration or result order.

All datasets must cover the same nonempty half-open evaluation interval and 15m
timeframe. Each Phase 5C clock requires at least 99 completed warmup candles and
complete contiguous coverage. Missing/extra symbols, wrong rule ownership,
misaligned intervals, insufficient warmup or missing candles fail before any
strategy evaluation. No forward-filling or interpolation is performed. Different
warmup counts are allowed if each meets the clock's requirement; the strategy
still receives exactly 99 completed candles plus its synthetic forming row.

### Boundary chronology and arbitration

At every shared opening timestamp:

1. Advance all three clocks together and verify equal opening times.
2. Obtain all three frozen-strategy decisions, before accessing current intrabar
   OHLC. Preserve all raw decisions, including NONE and blocked signals.
3. Process an existing position's candle through unchanged Phase 5D execution.
4. Determine eligibility from whether a position WAS HELD AT THE OPEN, not from
   the position state after learning the candle's exit outcome.
5. If eligible, consider actionable signals in fixed BTC/ETH/ADA order. Attempt
   sizing/entry through Phase 5D. A sizing failure is REJECTED with its reason;
   the next actionable symbol can be considered. The first tradeable signal wins.
   Once accepted, every lower-priority actionable signal is BLOCKED_PRIORITY.
6. Process the accepted position's entry candle using Phase 5D protections.
   Even if it closes immediately, arbitration does not repeat in this candle.

This uses a single position slot across the whole portfolio. When held at the
open, every actionable signal is BLOCKED_OPEN_POSITION, including the held
symbol's own signal. This remains true for opening-gap exits, intrabar exits and
an exit causing insolvency. NONE is recorded as a non-actionable NONE disposition,
not labeled as blocked. Earliest possible re-entry is the next valid opening
boundary. No current candle's exit can retroactively enable an opening entry.

Only signals and each candidate's current opening price/rules/equity participate
in arbitration. Current high/low/close/volume and later outcomes never choose a
winner. Full candles stay within simulation; Phase 5C event/frame construction
is unchanged. Existing Phase 5D entry, protection, gap, dual-touch, cost, rounding,
quantity and P&L functions are reused without modification.

### Shared realized equity

Portfolio equity starts at config.starting_equity (baseline 1,000 USDT). For
each closure, exactly once:

```text
equity_after = equity_before + closed_trade.net_pnl
```

The next accepted trade receives this updated equity for Phase 5D sizing (baseline
1% risk, maximum 3x leverage). Portfolio records use a CLOSED-TRADE realized
balance: while a position is held, they do not subtract its entry fee separately
or mark unrealized P&L. Entry fees are already reserved by Phase 5D's margin sizing
and included in closed net P&L; deducting them again would double-count them.
Thus this balance is not available cash or a mark-to-market account-equity series.
Phase 5D's standalone cash-debit timing is unchanged; closed results agree.

Nonpositive equity after closure records INSOLVENT and ends replay without future
entries. Losses are preserved without clipping. Signals from that final boundary
remain recorded and blocked by its opening position. This is deterministic
insolvency handling, not liquidation modeling.

At the last evaluation candle, a remaining position closes through the existing
Phase 5D END_OF_DATA function using its own symbol's completed close and exit
costs. Its net result updates portfolio equity once. Earlier SL/TP closure takes
precedence. Terminal state is COMPLETED or INSOLVENT, with no position remaining.

### Records and fingerprints

Immutable BoundaryRecord values contain opening timestamp, equity before,
three DecisionRecords in priority order, winner if any, the created Position,
the closed ClosedTrade, equity after, position symbol before/after and state
after (OPEN/FLAT/INSOLVENT). Each DecisionRecord retains the exact Phase 5C
Decision, disposition (NONE/ACCEPTED/REJECTED/BLOCKED) and reason where applicable.
The winner is the accepted candidate, not a higher-priority rejected candidate.
These records reconstruct entries, closures, blocked signals and equity changes
without live database identifiers. Equity before applies to all three opening
decisions; an intrabar closure's equity after is never reused at that same open.

PortfolioResult retains the config, symbol rules and dataset fingerprints in
fixed order, boundary records, final equity, terminal state and portfolio/execution
versions. Its fingerprint hashes canonical contents, including Decimal values
as canonical strings. No random IDs, wall-clock time or container insertion order
affects it. As accepted previously, future dataset edits may change provenance
and whole-run fingerprints without changing earlier strategy-visible decisions.
Results continue to state that funding and liquidation are excluded and that
these are not complete Futures account returns.

### Validation and next scope

Synthetic tests cover BTC/ETH/ADA arbitration, all mapping permutations, open
position blocking, intrabar/gap exit boundaries, entry-candle exit without a
second entry, sizing after wins/losses, insolvency, alignment/gap/warmup failures,
EOD accounting, rejected candidates, per-symbol prices/rules, one-position
lifecycle reconstruction, immutable source data and deterministic replay.
Look-ahead tests compare actual Phase 5C decisions, verify all three evaluations
precede intrabar processing, and show changed outcomes cannot change earlier
visible signals or opening arbitration. Scripted signals in selected scheduling
tests are test-only; production always uses the frozen strategy adapter.

Phase 5F remains unimplemented: aggregate metrics and reporting (including
profit factor, drawdown and any equity-curve conventions). This phase adds only
reconstructable portfolio records and fingerprints, not performance analysis.
No historical downloader, optimization, Strategy Lab, live execution, Binance
calls or bot trading entry point is added. Existing Phase 1/5B/5C/5D code/tests
remain unchanged; full-suite legacy startup tests still use their fake client.
