# Phase 6D proposal — disciplined DEVELOPMENT investigation

Design only, based on the closed Phase 6C evidence at
`a14c4c0bde9f9c01c0458388c96a647ece6a5518`. No implementation,
experiment execution, new registration, data acquisition or promotion is authorized
by this document. Proposed parameters require approval before implementation.

Phase 6D0 update: the operator separately authorized the history-only acquisition
described below. It is complete; no A/B/C/D implementation or execution occurred.

## Budget and frozen controls

Exactly four investigation lines: A ribbon filter, B breakout trigger,
C ATR protection, and D accepted-trade diagnostics. A–C are **three additional
development configurations/trials**. D is one separately recorded analysis, not a
fourth strategy trial. The existing Phase 6C 4h EMA 9/21 result is the reference,
used from its immutable artifacts and never rerun. If approved and subsequently
executed, the cumulative strategy-trial count becomes six, not three. These are
distinct configurations, not statistically independent observations.

There is no A×B×C combination, grid, EMA-span tuning, symbol exclusion or direction
pruning. Register all three exact specifications before seeing any new result.
Retries preserve identity and failed-attempt history; changed parameters require
a separately approved budget and count as another trial.

All experiments: `[2024-01-01T00:00:00Z, 2025-01-01T00:00:00Z)`, 4h signals,
15m execution, BTCUSDT/ETHUSDT/ADAUSDT, BTC→ETH→ADA priority, LONG+SHORT,
one global position, flat start at 1000 USDT, 1% risk, maximum leverage 3x.
Fee 0.0005/fill and adverse slippage 0.0002/fill remain research assumptions;
research-rules-v1 remains static and retrospective. Funding/liquidation remain
excluded. A and B retain fixed SL 1.5% / TP 3%; only C changes protections.
Entry remains the next signal-candle open with adverse entry slippage. Preserve
entry-candle protections, stop-first dual touch, adverse stop gaps, no favorable
TP-gap improvement, no same-boundary re-entry and final completed-close EOD exit.

2024 has already been observed. Every additional investigation increases
overfitting risk. 2025 stays LOCKED_PREVIOUSLY_OBSERVED_VALIDATION and cannot
become fresh validation if used for tuning. 2026 stays untouched TRUE_HOLDOUT.
History-only warmup generates no decisions, trades or scores.

## Common information and initialization contract

Let `i` be the 4h candle opening now; `k=i-1` is the latest completed candle.
No eventual H/L/C/V of `i` or later candle is available to a decision. All
features use completed candles through `k`. All comparisons are evaluated at the
same opening `i`; there is no extra delay or intrabar signal evaluation.

The original EMA9/21 trigger remains exactly its existing 99 completed rows
`i-99..i-1` plus synthetic row `i` (OHLC=current opening price, volume=0),
with pandas float64 `ewm(adjust=False)` and the existing -3/-2 crossover test.
Do not expand this trigger's EMA history when adding longer feature history.

For the new EMA20/50/100/200 features, propose a fixed **600 completed 4h
candles**, `i-600..i-1`, recalculated at each decision. Each EMA uses
`adjust=False`, alpha=2/(span+1), first close as seed, no cached full-history EMA.
This fixes initialization and bounds the initial EMA200 seed's weight after
599 updates to roughly 0.25%; it does not claim an infinite-history EMA.
The 600-row choice is an initialization rule, not a tested trading parameter.
Convert prices only at the indicator boundary; fingerprints retain canonical
data and the precise feature/seed definitions.

**Phase 6D0 warmup dependency resolved:** prior December coverage supplied 2,976
15m / 186 complete 4h candles per symbol. The separately authorized acquisition
added precisely 6,624 15m / 414 4h candles per symbol. The frozen history is
`[2023-09-23T00:00:00Z, 2024-01-01T00:00:00Z)`: 9,600 contiguous 15m candles
and exactly 600 complete, UTC-aligned 4h candles per symbol. No earlier start was
needed. Daily September 23–30 and monthly October/November public USD-M Futures
archives were downloaded; existing monthly December archives were reused.
All 33 archives passed official SHA-256 checksum verification, including the
three reused archives. Validation found zero gaps, duplicates or incomplete
candles. Twelve-column normalization, strict source order/spacing, OHLCV Decimal
validation and exact 16-component aggregation reused existing data functions.

The 600-candle choice remains an EMA200 stabilization policy, not a tuned
strategy parameter. The first 2024 opening has 600 completed history candles;
the last closes at 2023-12-31T23:59:59.999Z. History packages explicitly contain
`role=HISTORY_ONLY`, `evaluable_boundaries=[]` and
`pre2024_evaluation_allowed=false`. Their provenance keeps DEVELOPMENT fixed at
`[2024-01-01, 2025-01-01)`. The baseline evaluation clock rejects the history-only
dataset metadata. No decision, trade or scoring is performed on this history.
Integration into the future versioned feature/decision API remains separate;
do not relabel these packages as the frozen Phase 6B2 evaluation views.

Artifacts: `artifacts/research-data/phase6d0-warmup-v1/`, ignored by Git. This
contains the bounded acquisition script, new raw archives/checksums, normalized
source CSVs, per-symbol `*-15m-history.json` and `*-4h-history.json`, and
`manifest.json`. Existing baseline/Phase 6B2/Phase 6C artifacts were not modified.
Manifest fingerprints exclude wall-clock retrieval time and local paths.

Frozen manifest: `da2a0ec1f169ff055ef5ba39ac20a6f921b544ac83ada837cb174c844b9708f4`.

| Symbol | 4h history-view fingerprint |
|---|---|
| BTCUSDT | `20cb2024d0d72f4f5fb9b62337c32e1c6db001790fbbdb03779dd488c47c7a34` |
| ETHUSDT | `66ee284131d05d63f37607b6353069a78bd2bfbed49e73c59921baddba87a153` |
| ADAUSDT | `d7edb589044e2f7c5bf02949bba3f98ddafcda89bfd73f350aff1dd249ce96e5` |

The manifest also records source/dataset/normalized-4h fingerprints and every
official archive/normalized CSV checksum. Saved fingerprints and boundary
separation were reverified after writing. No strategy parameter changed, no
Phase 6D experiment ran, and no 2025 or 2026 candle data was accessed.

## A. Regime filter — MA ribbon

Question: does the existing crossover work better when a broader trend agrees?

- Trigger: unchanged EMA9/21 4h crossover.
- LONG passes only when `EMA20[k] > EMA50[k] > EMA100[k] > EMA200[k]`.
- SHORT passes only when `EMA20[k] < EMA50[k] < EMA100[k] < EMA200[k]`.
- All ribbon EMAs use the common 600-row completed 4h feature window.
- Equality at any adjacent pair, mixed ordering or unavailable history blocks
  the trigger. Invalid/nonfinite input is an integrity error, not a quiet NONE.
- Trigger and regime are checked at the same `i` opening. A blocked trigger is
  recorded as FILTERED_REGIME, not delayed until alignment appears. No signal
  is carried across boundaries. No trigger means NONE.
- Protections, sizing and all financial settings stay fixed.

Rationale: one transparent hierarchy of short-to-long trend measures separates
trigger timing from broader directional agreement. Use exactly the supplied
20/50/100/200 ordering; no alternate spans/orders, slope condition or threshold.

## B. Breakout plus trend

Question: is the weakness specific to crossover timing rather than 4h trend
following? This is one alternative family, not a ribbon/breakout combination.

- Lookback: **20 preceding completed 4h candles**, excluding tested candle `k`.
- `U[k]=max(high[k-20..k-1])`; `L[k]=min(low[k-20..k-1])`.
- Raw LONG breakout: `close[k] > U[k]`; raw SHORT: `close[k] < L[k]`.
- Generate an event only on false→true in the same raw direction: compare with
  the raw breakout at `k-1` using its own preceding channel `k-21..k-2`.
  Repeated breakout closes are not repeated entry events. A later reset and
  new breakout may produce another event. Do not carry blocked events forward.
- One broad filter: LONG requires `close[k] > EMA200[k]`; SHORT requires
  `close[k] < EMA200[k]`, using the common 600-row feature window.
- Equality at channel or EMA boundary does not qualify. Evaluate at `i` open;
  a wick alone is not a close-confirmed breakout. Both directions cannot qualify
  simultaneously for valid OHLC data; reject contradictory states.
- Retain SL 1.5% / TP 3% and every other frozen financial control.

Rationale: 20 bars gives one short, explicit 80-hour channel; EMA200 gives one
much broader directional reference. These are simple predeclared horizons,
not values inferred from Phase 6C's profitable subsets. No alternate channel,
lookback, volatility threshold or trend-filter variant is included.

## C. Volatility-adaptive protection

Question: does a fixed percentage exit fit the 4h entry horizon poorly?
Entry remains the exact existing EMA9/21 4h signal, without A or B filters.

- ATR period **14**, Wilder recurrence. Use completed candles `i-100..i-1`:
  the first is a prior-close anchor, producing 99 TR observations for
  `i-99..i-1`. `TR[j]=max(H[j]-L[j], abs(H[j]-C[j-1]), abs(L[j]-C[j-1]))`.
  Seed ATR with the arithmetic mean of the first 14 TRs, then apply
  `ATR=(13*prior_ATR+TR)/14` through `i-1`. Recompute this fixed window at each
  opening in the existing fixed 50-digit Decimal arithmetic context.
- Freeze entry ATR; stop distance **2×ATR14**, target distance **4×ATR14**.
  This is nominal 2R before costs/rounding. No trailing, breakeven move,
  volatility cap, fallback percentage stop or later ATR update.
- Anchor both levels to actual slipped entry fill, not the signal close.
  LONG: stop entry−2ATR, target entry+4ATR. SHORT: stop entry+2ATR,
  target entry−4ATR. Round away from entry: LONG stop down/target up;
  SHORT stop up/target down, using each symbol's frozen tick.
- Reject nonpositive ATR, nonpositive levels, invalid side relationships,
  or filters that prevent a valid position; do not silently substitute levels.
- Preserve 1% sizing semantics using actual rounded stop distance, modeled
  adverse stop fill and applicable entry/exit fees in planned stop loss, with
  existing leverage, balance, quantity-step and notional constraints. Gaps may
  exceed planned risk; 1% is not a guaranteed maximum realized loss.

Rationale: a single volatility scale tests exit-horizon mismatch while preserving
nominal 2:1 reward/risk. ATR14 with 2/4 multiples is a simple fixed proposal,
not a fit to observed trades. Known December coverage is sufficient for its
100 completed 4h candles, but an explicitly fingerprinted history view is needed.

## D. Concentration/regime diagnostics — no new strategy

Use only the **174 accepted trades** in the immutable Phase 6C 4h result and
authorized 2024/history-only candles. No simulated alternative portfolio,
ADA-only/SHORT-only experiment, excluded symbol or hypothetical blocked profit.
Analyze all symbols/directions with the same definitions before spotlighting
the known ADA SHORT subset. Its observed success makes this analysis post hoc.

Predeclare features at each accepted entry's opening, using only prior history:

- A's ribbon category: ascending, descending, mixed/equal.
- EMA200 slope: `EMA200[k]/EMA200[k-6]-1`, both values from the same 600-row
  recurrence available at entry. Also retain direction-signed slope.
- C's ATR14/close[k], and distance `close[k]/EMA200[k]-1`.
- Trend persistence: fraction of the last 20 close-to-close changes strictly
  matching trade direction; unchanged closes count zero, denominator stays 20.
- B's raw breakout/event and broad trend-filter states.

Outputs: deterministic per-trade feature table; complete symbol×direction
counts/net/gross/fees; quarter/month exit-attributed totals; pooled one-feature
summaries plus symbol/direction breakdowns; sparse-group labels. For ATR/price,
use fixed bands `[0,1%)`, `[1%,2%)`, `[2%,4%)`, `[4%,infinity)`. Slope and
distance use negative/zero/positive categories; persistence bands `[0,0.5)`,
`[0.5,0.75)`, `[0.75,1]`. No quantile fitting, threshold search or Cartesian
feature-bin combinations. Continuous values remain available for audit.

Outcome diagnostics reuse recorded holding-duration bounds and conservative
MFE/MAE conventions: include only fully observed exposure bars preceding the
exit bar; do not use unknowable post-exit extremes. Same-bar entry/exit has no
fully observed intrabar excursion and must be unavailable, not zero. EOD CLOSE
may include its full bar when exposure spans it; OPEN exits exclude their bar.
Record exact eligibility of bars and missingness. Intrabar exits have time bounds,
not invented timestamps. Report cost and calendar concentration without summing
overlapping groups as independent loss sources.

Look for repeated direction-consistent associations across symbols and quarters,
but treat them as descriptive. Accepted trades are selected by the strategy,
global occupancy and priority; they cannot establish causal regime effects or
profits of blocked signals. A discovered filter is a new hypothesis requiring
another budget, not an amendment to A–C.

## Eligibility and later robustness checks

Retain positive net expectancy, net PF >1, all four quarters active, at least
3/4 positive net quarters, maximum candle-close MTM DD strictly <25%, and no
integrity/reconciliation failure or insolvency. Below 100 completed trades is
WEAK_EVIDENCE only if the other gates pass; otherwise INELIGIBLE. No weighted
score, relaxed gate or automatic promotion.

Before any future promotion, separately review:

- Quarter stability: all quarter counts/net outcomes, sign consistency and
  whether aggregate gains depend on one quarter.
- Month concentration: monthly net results, largest positive-month share of
  total positive-month P&L, and best-month contribution relative to annual net
  (undefined ratios explicitly labeled). Subtractions are attribution, not
  hypothetical re-executed portfolios.
- Symbol/direction concentration: all six cells, sample sizes and dependence
  on one cell; partitions overlap and must not be summed across categories.
- Cost sensitivity: future separately authorized stress cases, frozen before
  execution and fully counted/disclosed. No extra cost runs in this budget.
- Parameter-neighborhood sensitivity: later, separately budgeted fixed neighbors,
  never selecting the best neighbor or silently replacing the original result.
- Evidence strength: total and subgroup counts, temporal clustering and shared
  market exposure. Meeting 100 is not proof of statistical independence.

These are diagnostic disclosures, not newly fitted acceptance thresholds.
No validation authorization is granted; any promotion remains an explicit
later operator decision under the unchanged locked-split policy.

## Framework reuse and required future additions

Reuse 15m data validation, 4h aggregation, typed decisions, global scheduling,
Phase 5D fill/protection processing, Phase 5F analytics, canonical reporting,
immutable artifacts and append-only ledger mechanics. Preserve existing default
paths and original `strategy.py`/live risk behavior.

The present registry admits one identity per signal timeframe and embeds the
three-concept policy; it **cannot register these variants as-is**. Design a
versioned Phase 6D batch/spec envelope binding the unchanged eligibility/access
policy, exact three new variant definitions, reference/ancestry and new feature
history. Do not edit old event payloads, IDs, policy or slot allocations. Prefer
a distinct versioned ledger under the research namespace, explicitly linked to
the prior three trials with a cumulative trial summary. Do not relabel a new
ledger as an unexplored research history.

Likely additions, subject to implementation review:

- `research/features.py`: typed completed feature window, ribbon/EMA/ATR/channel
  calculations and initialization fingerprints. Preserve the trigger's 99 rows.
- `research/variants.py`: explicit A/B adapters and C's unchanged entry adapter
  with immutable entry-ATR metadata; no implicit strategy selection.
- `research/protection.py`: typed fixed/ATR protection plan. Extend the execution
  boundary minimally so the existing risk/sizing kernel consumes a resolved
  plan; extract shared arithmetic if needed rather than copying the engine.
  Current SimulationConfig rejects non-fixed percentages, so C cannot be
  implemented by merely passing different config values.
- `research/concentration.py`: D's read-only feature/outcome join and reports.
- Versioned registry/runner/report support and focused offline tests. Feature,
  protection and diagnostic versions/hashes must enter identities explicitly.

All financial differences for C must be attributable to the approved protection
plan. A/B must retain exact existing financial behavior. Do not extend live
SQLite/runtime state or alter Phase 6C/baseline artifacts.

## Separate execution-realism backlog

Future work: funding timing/rates, isolated versus cross collateral, maintenance
margin tiers, liquidation-price computation, liquidation versus stop priority
when gaps cross both, and leverage/collateral interactions. These can materially
change solvency and realized returns, but test account mechanics rather than
signal discrimination. Mixing them into A–C would confound the comparison.
Design/freeze a separate realism layer and data contract before any such runs;
do not infer safe leveraged trading from liquidation-excluded DEVELOPMENT output.

## Small implementation phases proposed for later approval

1. Approve parameters, initialization, history access and versioned trial budget.
2. Add spec/ledger envelope and completed feature views; validate any separately
   authorized 2023 history without signals. Test split denial and fingerprints.
3. Implement A/B with synthetic equality, channel-exclusion, no-lookahead and
   exact baseline-trigger equivalence tests. No market experiments.
4. Implement C's explicit protection boundary; test sizing, rounding, gaps,
   adverse fills, rejected invalid levels and unchanged default-path equivalence.
5. Implement D and versioned reports using synthetic accepted-trade fixtures;
   verify duration/excursion bounds, partitions and immutable writes.
6. Review and freeze all three registrations plus D's analysis specification.
   Execute only after a separate authorization, without modifying later variants
   after viewing earlier outcomes. No validation/holdout access.

## Open approval questions

The proposals above are exact rather than configurable alternatives. Remaining
decisions are approval for strategy implementation/execution using the 600-row
EMA initialization, the close-confirmed false→true breakout definition, the ATR seed
and 2/4 multiples, and the versioned budget/ledger migration approach. History
availability is now resolved by Phase 6D0. No shorter window, delayed evaluation
start or alternative parameter will be substituted if a dependency is missing.
All remain design questions; no user action is required to finish this proposal.
