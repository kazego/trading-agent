# Phase 6E — frozen candidate selection

The operator selects exactly one candidate for the next validation stage:
**phase6d-c-atr-v1**. This is a governance decision only. Validation has not
begun, no market-data access is granted here, and executable access gates remain
unchanged. B is not authorized for the next validation stage.

The authoritative deterministic record is [phase6e-selection.json](phase6e-selection.json).
It binds the full tested trial specification, source/software/configuration and
data identities, completed result, comparison, policy, rationale and weaknesses.
Its selection fingerprint is:
`92ee399eccbb27b0b6cfa32efbcfea1e9e837569bf61befd6d777eff10014d84`

## Frozen candidate

- Unchanged EMA9/21 crossover; 4h signals and 15m execution.
- Strategy: `EMA_CROSSOVER` / `research-ema-opening-v1`.
- Protection: `WILDER_ATR_PROTECTION` / `atr14-2x-4x-v1`.
- Wilder ATR14, 2 ATR stop, 4 ATR target, frozen at entry; no trailing.
- Initial equity 1,000 USDT; 1% planned equity risk using actual rounded stop
  distance; maximum leverage 3x.
- BTCUSDT → ETHUSDT → ADAUSDT priority; maximum one active position total.
- LONG and SHORT retained, with no symbol or direction pruning.
- Existing modeled fees (0.0005 per fill), adverse slippage (0.0002 per fill),
  tick rounding, gap, intrabar/dual-touch, arbitration and EOD semantics unchanged.
- No EMA/ATR tuning or execution-assumption changes.

## Rationale and retained weaknesses

| Preserved DEVELOPMENT metric | EMA9/21 4h reference | Selected C |
| --- | ---: | ---: |
| Net return | 3.9540% | 9.0599% |
| Net P&L, USDT | 39.5398 | 90.5992 |
| Net PF | 1.0367 | 1.2359 |
| Net expectancy/trade, USDT | 0.2272 | 1.4852 |
| Maximum candle-close MTM drawdown | 14.0183% | 13.1965% |
| Positive quarters | 2/4 | 3/4 |
| Trades | 174 | 61 |
| Filled notional, USDT | 211,206.6520 | 32,704.2445 |
| Modeled fees, USDT | 105.6033 | 16.3521 |
| Recorded fill slippage, USDT | 42.2413 | 6.5409 |

Selection considers return, risk, breadth and friction together; it is not
based solely on highest P&L. C passes the frozen eligibility gates, but its
stored **WEAK_EVIDENCE** designation is retained: only 61 trades, below the
nonbinding 100-trade evidence target. Only 6/12 months were positive.

Concentration remains: BTC LONG and ETH SHORT supplied most positive attribution;
the other four symbol×direction groups were net negative. Temporal concentration
also remains. These findings do not authorize pruning or new trading rules.

Funding and liquidation are not modeled. Transaction costs remain research
assumptions, not exact historical/account costs. MTM drawdown uses candle closes,
not worst intrabar excursions. Static research symbol rules are not asserted
historically identical. Selection on observed DEVELOPMENT data is not independent
validation evidence.

## Nonselected trials

**B: ELIGIBLE_BUT_NOT_SELECTED.** It passes frozen DEVELOPMENT eligibility,
but does not improve the reference on return, expectancy, PF or drawdown.
Turnover is higher, and symbol/temporal concentration is material.
B is not authorized for the next validation stage and must not run on validation data.

**A: rejected/ineligible.** Its negative expectancy, net PF below one and
insufficient positive-quarter breadth remain unchanged. No result is rewritten.

## Data roles and research discipline

Exactly six strategy configurations have examined 2024: Phase 6C EMA9/21
15m, 1h and 4h; Phase 6D A, B and C. Diagnostic D counts as zero.
Phase 6E creates zero configurations; no seventh configuration is authorized.

- **2024 DEVELOPMENT:** heavily observed and closed to further strategy development.
  Further tuning is prohibited under the current research plan.
- **2025 LOCKED_PREVIOUSLY_OBSERVED_VALIDATION:** previously observed in
  baseline/research work; not genuinely unseen holdout data. No Phase 6E access,
  reading, evaluation or execution.
- **2026 TRUE_HOLDOUT:** untouched, endpoint unset, reserved for later true holdout
  confirmation. No access, reading or evaluation.

C is frozen before validation. No adaptation is permitted after observing 2025
results. If C fails 2025, preserve that failure; it may not be retuned and rerun
under the same validation claim. No validation run begins in this phase.

## Inherited identities and architecture

- Selected computational trial:
  `689c90ddee96b7239b12e53714e05c024e7b63bc30d6745d0d54b11ef561b1be`
- C result:
  `46e37dd09f8fad5ed8cb67e7732e5ec3286757255d169296128aa8246f454f1f`
- Phase 6D6 comparison:
  `c810e5ab314a4132d4c60f4ea37dc87d5baec7de1d304935d027d406a45373ee`
- Frozen trial plan:
  `4172bd2610e0c51d8ef4df73aad21f3afe06656ca5f71e1ae5441ad650ca785b`
- Full Phase 6 policy:
  `2642052de313ece07b300791ab1663ff60fc8368d9d7d0059934cde75d4dac99`
- Eligibility subsection:
  `e8f4c0d736c8a8c29e3dc26e61aa3bf194e519eeb9db16e614190f19054cb348`

The existing registry is append-only and the Phase 6D access path remains
DEVELOPMENT-only. Its validation/holdout methods reject Phase 6D grants.
No registry method or execution infrastructure is changed or bypassed.
This versionable selection record is the smallest governance addition; it does
not register another strategy, grant runtime access, or create an attempt.

Canonicalization uses existing sorted, compact ASCII JSON, Decimal-compatible
strings and SHA-256. Hash the complete JSON object excluding its
`selection_fingerprint` field. Local paths, retrieval time and wall-clock time
are excluded. The inherited ledger snapshot remains unchanged:
`56da9bbf4d8a2f0158c5cba7e26face152ec5bbc4c1756900028e1c93addf8fe`

Verification used existing preserved result/manifest/scorecard artifacts and
registry metadata only. No code was introduced, so no unit tests or full suite
were run. Canonical serialization, selection/evidence fingerprints and frozen
trial binding were checked directly. No simulations, market-data reads,
parameter changes, new trials, validation, live/demo execution or commit occurred.

