# Phase 6D2 — explicit 4h signal strategies A and B

Implements two frozen strategy families only. No market experiment, saved-result
diagnostic, ATR protection, scheduling change or financial execution is added.
Original `strategy.py`, the Phase 6B/6C research adapter and scheduler, Phase 6D1
features and all financial modules remain unchanged.

## Typed API

Select `RibbonStrategy(feature_view)` or `BreakoutStrategy(feature_view)`
explicitly, then call `evaluate(feature_snapshot)`. A typed authorized
`FeatureView` is mandatory; neither a default strategy nor parameter overrides
are accepted. `FeatureStrategy` is the feature-input counterpart to the existing
opening-window research protocol. Output `VariantDecision` subclasses the
existing `SignalDecision`, adding the feature fingerprint and raw trigger.
Its frozen parameter record identifies the actual trigger/filter instead of
pretending breakout uses EMA9/21 parameters.

The strategy verifies a supplied snapshot by recomputing Phase 6D1 features from
its bound view at that timestamp and requiring exact equality. This prevents
fabricated or modified feature values from entering through ordinary typed input.
The view enforces 2024 DEVELOPMENT, exact 4h boundaries and history-only warmup.
Raw dictionaries, candles and feature windows are not strategy inputs. Private
Python introspection/monkeypatching is outside this accidental-access boundary;
tests use explicitly mocked feature calculations only for branch isolation.

Existing Phase 6B3 `ResearchSchedule`, Phase 6B4 runner and EMA-specific execution
conversion remain unchanged and do not automatically accept/select these families.
Future versioned integration must handle their exact parameter records explicitly;
this phase does not route either strategy into financial execution or registration.

## A: EMA crossover with ribbon

Family `EMA_CROSSOVER_RIBBON`, version
`ema9-21-ribbon20-50-100-200-4h-v1`.

- LONG trigger: previous EMA9 <= previous EMA21 AND latest EMA9 > latest EMA21.
- SHORT trigger: previous EMA9 >= previous EMA21 AND latest EMA9 < latest EMA21.
- EMA9/21 retain the 99-completed-close convention; previous/latest correspond
  to -3/-2 in the historical/forming 100-row frame.
- LONG requires strict EMA20>50>100>200; SHORT requires strict reverse. These
  features use the separate 600-completed-close window. Equality/mixed alignment
  is NEUTRAL and blocks. Trigger and filter use the same decision boundary.
- No trigger: NONE / NO_CROSSOVER. Trigger without matching regime:
  NONE / FILTERED_REGIME. Accepted signal: CROSSOVER_WITH_RIBBON.

## B: fresh breakout with EMA200 trend

Family `FRESH_BREAKOUT_TREND`, version `fresh-breakout20-ema200-4h-v1`.

- Use the Phase 6D1 fresh bullish/bearish breakout states, lookback exactly 20.
  The tested completed candle is excluded from its preceding high/low channel.
- Fresh bullish plus completed close above EMA200 gives LONG; fresh bearish
  plus close below EMA200 gives SHORT. Equality blocks.
- No fresh event, including repeated true breakout states: NONE /
  NO_FRESH_BREAKOUT. Fresh event without matching trend: NONE / FILTERED_TREND.
  Accepted signal: BREAKOUT_WITH_TREND. Contradictory fresh directions fail closed.

Both strategies are stateless. Neither alignment alone nor a newly matching
filter emits an earlier blocked event. Every boundary is evaluated independently;
there is no pending-trigger queue, deferred entry or repeated stale signal.

## Identity and information boundary

Decision identity hashes exact symbol, 4h timestamp, LONG/SHORT/NONE output,
raw trigger/reason, family/version, frozen parameters, visible-window and feature
fingerprints, completed history references, strategy source SHA-256 and software
versions. Parameters contain the complete frozen Phase 6D1 feature policy.
No wall clock, paths, random identifier or future dataset provenance is included.
The 600 timestamps identify the complete feature history; only the final 99
contribute to A's trigger. These are decision records, never trade intents.

Normal verification selects only completed history before T. Current forming
H/L/C/V, later 15m components and future 4h candles are absent from the snapshot.
Equal visible data yields equal decisions even when whole-view provenance changes.
Issued decisions and nested parameter objects are immutable.

## Offline verification and scope

Synthetic branch tests cover LONG/SHORT, wrong/equal filters, alignment without
events, stale events, blocked events not emitted later, deterministic identity,
typed input/split/timeframe rejection and explicit selection. Unmocked feature-
to-strategy tests produce LONG for both families and prove identical decisions
after forming/future mutations, including a changed later 15m component. An
existing EMA adapter equivalence test and the full prior suite protect regressions.

No saved 2024 dataset is evaluated, no Phase 6C result is used to select signals,
and no 2025/2026 data is accessed. No new dependencies. No C/D logic or Phase 6D3
work is included. Execution/registry integration requires its own later scope.
