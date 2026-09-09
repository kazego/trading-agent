# Phase 6D1 — deterministic completed 4h features

Feature infrastructure only: no A/B/C adapter, trading signal, protection price,
trade, saved-result diagnostic or research experiment is implemented or run.
Existing baseline and Phase 6B/6C modules are unchanged.

## Explicit API and access boundary

1. `load_history(directory, symbol)` reads only the explicitly selected Phase
   6D0 manifest and that symbol's 4h history JSON. It verifies the hard-coded
   frozen manifest/view fingerprints, HISTORY_ONLY flags, candle content hash,
   symbol, chronology and exact 600-candle interval. No network or default path.
2. `FeatureView(history, development)` requires an issued `HistoryPackage` and
   a typed Phase 6B2 `SignalDataView`. It enforces the existing DEVELOPMENT
   authorization before inspecting supplied development candles, exact 4h/15m
   metadata, policy version, chronology and content/provenance hashes. The
   shared final 99 warmup candles must match exactly. History content is checked
   again to reject dataclass replacement after loading.
3. `view.at_open(T)` issues an immutable `FeatureWindow` containing exactly
   `T-600*4h .. T-4h`. It permits only aligned openings in
   `[2024-01-01, 2025-01-01)`. HISTORY_ONLY, between-boundary, validation and
   holdout timestamps fail closed. The view owns future historical data privately;
   only the issued window belongs at the feature calculation boundary.
4. `calculate_features(window, FeatureParameters())` returns an immutable
   `FeatureSnapshot`. Raw lists, unissued windows, mismatched fingerprints,
   incomplete history and non-frozen parameters are rejected.

This is an accidental-access/information boundary, not protection against
deliberate Python introspection or monkeypatching. Offline tests explicitly use
private issuance and patched hashes for synthetic fixtures; production has no
test-mode switch. This module does not load DEVELOPMENT files itself or bypass
the existing data-loading policy. Caller-supplied views must already be authorized.

No feature here needs the current opening price. The window therefore exposes
neither it nor current/future H/L/C/V or underlying forming 15m components.
No pre-2024 candle is an evaluation boundary.

## Frozen calculations

Let `i` be the current 4h opening and `k=i-1` the latest completed candle.

**EMA9/21:** recompute pandas float64 `ewm(adjust=False)` over completed closes
`i-99..i-1`, seeded at their first close. Return both last two EMA values. These
equal the -3/-2 values of the original 100-row historical/forming frame: appending
a forming row cannot change earlier EMA outputs. Do not use the 600-row EMA for
these spans and do not generate a crossover signal.

**EMA20/50/100/200:** recompute on all 600 completed closes, first-close seed and
`adjust=False`. Return previous/latest values. No other spans are accepted; no
full-history cache or optimized recurrence replaces pandas semantics. Ribbon is
BULLISH for strict 20>50>100>200, BEARISH for strict reverse, otherwise NEUTRAL,
including any equality. Latest completed close versus EMA200 uses the same
float64 indicator boundary: above/below/equal yields BULLISH/BEARISH/NEUTRAL.
These are feature states, not order directions or combined strategy decisions.

EMA outputs are represented as Decimal from the round-trip float string for
canonical serialization, not presented as arbitrary-precision EMA calculations.
Source financial values remain Decimal. Nonfinite/overflow/underflow-to-zero
indicator prices and invalid EMA outputs fail closed.

**Breakout20:** test `close[k]` against max high/min low of `k-20..k-1`.
For the prior state, test `close[k-1]` against `k-21..k-2`. Thus array slices
are `[-21:-1]` and `[-22:-2]`, never including the candle being tested. Equality
is false. Fresh bullish/bearish means current raw state is true and the prior
same-direction raw state is false. Consecutive true states are not fresh events.
Return both states, transitions, latest completed close and current reference
high/low; do not combine with the trend filter or emit a trade signal.

**Wilder ATR14:** take the final 100 completed candles `i-100..i-1`; the first
provides the prior-close anchor. Compute 99 true ranges for `i-99..i-1` as
`max(high-low, abs(high-previous_close), abs(low-previous_close))`. Initialize
with the arithmetic mean of the first 14 TRs, then apply 85 updates
`(13*previous_ATR+TR)/14`. Use a fresh 50-digit Decimal context, independent of
ambient caller precision. No stop, target, sizing or execution behavior changes.

## History and deterministic identity

Phase 6D0 remains exactly `[2023-09-23, 2024-01-01)`, 600 completed 4h candles per
symbol. Its manifest fingerprint is
`da2a0ec1f169ff055ef5ba39ac20a6f921b544ac83ada837cb174c844b9708f4`.
The three symbol hashes are frozen in `research/features.py` and recorded in
`docs/phase6d-design.md`. This phase verifies those packages, without calculating
features on saved 2024 market data.

Snapshot identity includes symbol, 4h timeframe, decision time, completed boundary,
visible 600-candle fingerprint, implementation version, frozen parameters,
feature values, normalized source-code SHA-256 and Python/pandas/numpy versions.
No wall clock, local path, retrieval time or random ID appears. Separate view
provenance records whole history/development hashes, but those do not enter the
snapshot identity: changing only future candles cannot change an earlier snapshot.
Source/version changes intentionally change snapshot identity.

## Verification and remaining scope

Offline synthetic tests cover immutable models, authorized parameters, EMA
equivalence and seed/window selection, ribbon/trend equality, breakout indexing
and transitions, gap TRs and Wilder initialization, Decimal-context independence,
first/rolling DEVELOPMENT boundaries, warmup/locked-split denial, overlap and
fingerprint corruption, and forming/future mutation invariance.

Later phases must explicitly consume these features in approved variant/registry
interfaces. Nothing here registers additional trials, extends locked access,
implements diagnostics D or automatically executes any strategy. No new dependency
is required; existing pandas/numpy are reused.
