# Phase 6D4 — preserved 4h DEVELOPMENT diagnostics

## Scope and preserved work

Diagnostic investigation D only. No new signals, strategy experiments, simulated
trades, variants, filters, symbol/direction exclusions or parameter tuning.
The completed Phase 6C 4h run was not rerun. Existing execution, risk, feature,
A/B and ATR policy implementations are unchanged. No 2025 evaluation or 2026
access occurred. Phase 6D5 has not begun.

The interrupted operation had already completed the diagnostic module, 17
synthetic tests, report.json/report.txt and assessment.json/assessment.txt.
Resume verified those diagnostic fingerprints and their implementation hash,
preserved all four artifacts, and completed this documentation. No repeated
source reconciliation or diagnostic generation was needed.

## Authoritative population and integrity

Source experiment:
`0c8d02a7bb3a2e9bb02c9e8b9c36e6c248581d7c95b2b3e3f91572faa7dd5851`

Source run:
`37de98cc08420c945dece486ec0295b144dc3824b44fad1705367d08f4d096b7`

Preserved result:
`726a515e0fbb95a9b3281afe5d179c55f0d89e6dd9c5d20d77f1796342de6632`

Ordered 174-trade population:
`7aae300aae0a45e7281054b9e83c4813238aac2b99ce8a9010bd99d3b1853d3b`

The module verifies the frozen manifest, recorded file SHA-256 values, full
result fingerprint and ordered closed-trade fingerprint. It extracts existing
BoundaryRecord.closed values; it never invokes the strategy or scheduler.
Every output record retains its existing decision ID and source-trade checksum.
Duplicate identities, wrong symbol/timeframe/boundary, non-2024 entry/exit times,
and financial mismatches fail closed. Exact population checks require 174 total
and 29 ADA SHORT trades, gross 189.93465862 and net 172.46507486225 USDT.
Source artifacts were verified again before publishing the completed report.

## Entry-known versus descriptive versus post-trade information

`entry` contains symbol, actual traded direction, entry boundary, feature/visible
fingerprints and these completed-only Phase 6D1 features:

- EMA20/50/100/200 strict bullish or bearish ribbon. Nonaligned equal values are
  EQUALITY_NEUTRAL; other nonaligned values are MIXED.
- Completed close relative to EMA200: BULLISH means above, BEARISH below,
  NEUTRAL equal, preserving the existing indicator comparison convention.
- EMA200 latest minus previous completed value in the same 600-candle window:
  RISING, FALLING or FLAT with no magnitude threshold; raw difference recorded.
- Wilder ATR14 / completed close, using the existing ATR and 50-digit Decimal
  arithmetic, without a float conversion for the ratio.
- Signed (completed close - EMA200) / completed close.
- Independent bullish/bearish breakout states: FRESH, STALE or FALSE, using the
  unchanged Phase 6D1 reference range and transitions. These are not Strategy B
  performance estimates.
- Entry UTC month and quarter, explicitly separate from exit attribution.

Only `FeatureView.at_open(entry_boundary)` reaches feature calculation: exactly
600 completed 4h history candles, excluding the current/forming bucket and all
later components. Each symbol uses its own validated view and frozen 6D0 history.
The local loader opens December 2023 and 2024 candle files only; the older
acquisition manifest is metadata and does not authorize other-year candle access.

`descriptive` contains volatility quartile membership. Cutpoints use linear
interpolation at (n-1)*p over all 174 accepted trades; ties enter the lower
partition. Approximate cutpoints are 0.016006889282, 0.018967628833 and
0.023794578569; exact Decimal strings are in JSON. These population-dependent
labels are not entry-known filters, optimized thresholds or strategy parameters.

`outcome` contains gross/net P&L, fees, exit reason/phase/boundary, exit UTC
month/quarter, duration bounds and conservative MFE/MAE. None enters `entry`.
Core month/quarter tables use UTC exit-candle opening attribution, matching Phase
6C. Duration uses the existing lower/upper bounds (15-minute uncertainty for
INTRABAR). Excursions reuse the existing convention: full candles strictly before
exit, then exit open/reference; exit H/L only when exit phase is CLOSE. Unknown
post-exit movement is excluded. These are conservative observations, not exact
intrabar extrema or entry filters.

## Tables and arithmetic

JSON includes all symbol, direction, symbol x direction, month, quarter and
regime x direction summaries. Every group reports trades, net wins/losses,
breakevens, win rate, gross/net P&L, gross/net PF, gross/net expectancy, fees,
median duration bounds and median conservative MFE/MAE percentages.

Each PF divides positive P&L by the magnitude of negative P&L on its own gross
or net basis. Undefined ratios have null values and explicit statuses. All
observed regime categories are compared across ADA SHORT, ADA LONG, BTC SHORT,
ETH SHORT, non-ADA-SHORT and all trades. No favorable categories are silently
selected; overlapping tables/cohorts must not be added as independent results.
ADA also has complete per-trade records, distance/duration/MFE/MAE distributions,
regime tables, exit tables and zero-filled month/quarter tables.

Concentration uses signed contribution / total net, so shares may exceed 100%.
Best/worst N use net P&L then decision ID; 5% means ceil(174*0.05) = 9 trades.
Exclusion figures subtract recorded attribution, without counterfactual execution.

## PREDECLARED DIAGNOSTIC findings

Overall net: 39.539766095443 USDT. ADA SHORT contributed 172.46507486225 USDT
(436.18%); the other 145 trades contributed -132.925308766807 USDT.

ADA SHORT: 17 wins / 12 losses, 58.62% win rate, gross PF 2.67335, net PF
2.42560, net expectancy 5.94707 USDT/trade and fees 17.46958375775 USDT.
Its 17 TP exits contributed 293.44214243393 net and 12 SL exits -120.97706757168;
no EOD exit. Median duration bounds are 405–420 minutes. Median conservative
MFE is 3.00105%, MAE 1.11106%; median signed EMA200 distance is -0.01385353556.

| ADA SHORT exit period | Trades | Net USDT |
| --- | ---: | ---: |
| Q1 | 8 | 3.23042057274 |
| Q2 | 6 | 48.49559240533 |
| Q3 | 9 | 69.03020032861 |
| Q4 | 6 | 51.70886155557 |
| January | 2 | 35.5687581869 |
| February | 0 | 0 |
| March | 6 | -32.33833761416 |
| April | 2 | 33.93147058527 |
| May | 2 | 7.56815834212 |
| June | 2 | 6.99596347794 |
| July | 6 | 46.39643666497 |
| August | 2 | 6.01301768268 |
| September | 1 | 16.62074598096 |
| October | 2 | 35.18008848508 |
| November | 2 | 8.06558211812 |
| December | 2 | 8.46319095237 |

ADA SHORT was positive in all four quarters and ten months. July, not October,
was its largest contributor. Longest nonprofitable sequence between winners:
five trades, over 35.4271 exit-boundary days. Longest time between profitable
exit boundaries: 51.2604 days, with no intervening ADA SHORT trades. These are
boundary-based descriptions, not invented intrabar timestamps.

Overall portfolio concentration differs from ADA's temporal persistence:

| Contribution | Net USDT | Share of total net |
| --- | ---: | ---: |
| Best month: October | 145.01502949545 | 366.76% |
| Best quarter: Q4 | 90.019679645289 | 227.67% |
| Best 5 trades | 98.80598282228 | 249.89% |
| Best 10 trades | 195.109526245736 | 493.45% |
| Best 5% (9 trades) | 176.029272053766 | 445.20% |
| Worst 5 trades | -57.094969659083 | -144.40% |
| Worst 10 trades | -113.12087079187 | -286.09% |
| Worst 5% (9 trades) | -102.01522642794 | -258.01% |

Subtracting October attribution leaves -105.475263400007 USDT; subtracting Q4
leaves -50.479913549846. These are not rerun portfolio results.

## POST-HOC OBSERVATIONS within the predeclared tables

The following highlighted cells are exploratory interpretations of the complete
tables, not newly authorized filters. Net expectancy in USDT/trade:

| Regime | ADA SHORT | BTC SHORT | ETH SHORT | ADA LONG |
| --- | ---: | ---: | ---: | ---: |
| Mixed ribbon | 6.960 (16) | -0.819 (6) | -2.367 (7) | 1.876 (7) |
| Below EMA200 / falling EMA200 | 6.721 (18) | -6.670 (9) | -1.856 (18) | -0.096 (11) |
| Descriptive ATR Q3 | 9.375 (14) | -9.658 (4) | -1.184 (9) | -1.820 (7) |

Parentheses are sample sizes. Similar regimes exist elsewhere but do not show
uniformly positive transfer. All regimes, including unfavorable ones, remain
available in the canonical report. EMA200 slope and close-relation signs are
algebraically related for one EMA update and are not independent confirmations.
Small cells and observational attribution cannot establish significance or cause.

## Descriptive classification

SYMBOL_SPECIFIC_CONCENTRATION for the overall positive result, and
TEMPORAL_CONCENTRATION for the overall portfolio. ADA SHORT alone was spread
across the year; the temporal label must not be misapplied to it. Evidence does
not justify a broad, consistently transferable regime association. No label
permits pruning symbols/directions, tuning or creating a trading rule.
Funding and liquidation remain excluded from the underlying simulation.

## Artifacts, tests and remaining limitations

Ignored immutable directory: `artifacts/research/phase6d4-diagnostics/`.
It contains canonical `report.json`, concise `report.txt`, and separately
fingerprinted `assessment.json` / `assessment.txt` for the labeled interpretation.
The assessment references the numerical report; it is not an automatic classifier.

Diagnostic fingerprint:
`b0c1ec6ea26a04a95a8a82ca48ed5a2c50c0b9d4e835a4224b4003296e8e7864`

Assessment fingerprint:
`c93118608d75039c4e146cbbfbcb5c5cf16d90ca181d2f5f03b92cc96afb39a5`

Identity includes source run/result/trade population, feature implementation and
version, dataset/view/history hashes, diagnostic version/specification and code
hash, plus deterministic records. No wall-clock timestamp or local path enters
computational identity. Existing or partial output directories block writes;
completed artifacts are never silently overwritten. Running
`python -m research.regime_diagnostics --diagnose-existing-4h` now refuses because
the preserved output exists; it must not be rerun merely to inspect the report.

17 new offline tests cover reconciliation, exact joins, forbidden boundaries,
slope, normalized ATR, signed distance, ribbon equality, PF/expectancy edge cases,
group ordering, cross-cohort reconciliation, descriptive quartiles, concentration,
calendar gaps, conservative excursions, namespace separation, no-lookahead,
deterministic identity, exclusive writes, corrupt sources and absence of signal/
execution/network calls. Full suite: 439 tests passed before interruption and all 439 passed in the
single final offline suite run on resume.
No dependencies changed. No unresolved implementation blocker was identified.
Observational uncertainty, sparse groups and unknown intrabar ordering remain.
