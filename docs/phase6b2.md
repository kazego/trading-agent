# Phase 6B2: deterministic research signal data

`research/data.py` adds data views only. It imports the frozen baseline data
validator and Phase 6B1 specification/policy, not a strategy, execution engine,
registry runner or network client. No research experiment or registry record is
created. Baseline Candle continues to accept only 15m.

## Models and aggregation

`AggregateCandle` is a separate immutable research type supporting 15m, 1h and
4h. `aggregate_bucket` requires respectively one, four or sixteen contiguous
validated baseline Candle components. Buckets are aligned to UTC epoch multiples
of 900000, 3600000 or 14400000 milliseconds. Open is the first open, high/low the
extremes, close the final close, and volume the exact Decimal sum. Close time is
bucket open plus duration minus one millisecond. Float/nonfinite financial values,
invalid OHLC, mismatched symbols/timeframes, duplicate/gapped/out-of-order or
misaligned components, and partial required buckets fail closed. No interpolation
or repairs occur. Decimal summation sizes its precision to the input digit span
and carries, independent of the caller's Decimal context.

`build_view` validates the source provenance and normalized-candle fingerprint,
actual contiguous coverage, duplicate/incomplete counts, symbol and timeframe,
development interval and completion cutoff. All required components must exist.
Extra source warmup is permitted but trimmed to exactly 99 signal candles. A
15m aggregate has exactly the same OHLCV/timestamps as its baseline component;
its research provenance/identity is intentionally distinct.

## Split and opening boundaries

Both the view builder and local loader enforce Phase 6B1's typed split policy.
Phase 6B2 intentionally offers only a DEVELOPMENT capability: exactly
[2024-01-01, 2025-01-01) UTC. Validation and holdout are denied before inspecting
supplied datasets. There is no validation bypass, even with a registry promotion
record in hand. Future validation loading must explicitly integrate that promotion
gate before broadening this capability. 2025 remains previously observed locked
validation; no 2025 historical candle files were loaded here. Holdout is denied.

`SignalDataView.at_open(timestamp)` accepts only aligned evaluation openings.
It rejects warmup timestamps, intrabucket timestamps and the exclusive end.
It returns a frozen `SignalOpening`: exactly 99 completed signal candles, the
completed boundary, current opening price and visible-window fingerprint.
It never returns current or future H/L/C/V. A future adapter can construct the
synthetic forming candle with OHLC all equal to this opening price and volume
zero; no adapter or EMA computation is implemented in Phase 6B2.

For a 4h opening at 12:00, the final completed bucket begins at 08:00 and ends at
11:59:59.999. The 12:00 bucket's eventual H/L/C/V are absent. Tests alter those
fields and later buckets without changing the opening view. Future consumers
must receive SignalOpening, not SignalDataView, which necessarily owns full
historical aggregates. This is an API boundary, not an introspection sandbox.

Warmup is accessible only as history. `evaluation_candles` excludes all 99 warmup
candles; `at_open` cannot evaluate warmup. No decision, trade or scorecard is
produced. Execution resolution remains 15m.

## Local source validation and available history

`load_local_development(historical_root, symbol)` checks the approved acquisition
manifest hash and opens only fixed December 2023 and January–December 2024
normalized filenames. Their bytes must match recorded SHA-256 checksums.
It calls unchanged Phase 5B ingestion for December coverage and for a newly
selected 2024 development dataset with 1584 underlying warmup candles. The
original selected baseline datasets/manifests are not rewritten. The acquisition
manifest contains other-year metadata, but those candle files are never opened.
Missing local files raise an explicit error; no downloading is implemented.

All three symbols have a complete December 2023: 2976 underlying 15m candles,
equivalent to 744 complete 1h buckets and 186 complete 4h buckets.

| Signal timeframe | Required underlying warmup | Required signal history | 2024 evaluation candles per symbol |
| --- | --- | --- | --- |
| 15m | 99 | 99 | 35136 |
| 1h | 396 | 99 | 8784 |
| 4h | 1584 | 99 | 2196 |

Warmup starts at 2023-12-30 23:15 for 15m, 2023-12-27 21:00 for 1h, and
2023-12-15 12:00 for 4h. All three symbols/timeframes supply exactly 99 completed
signal candles at the first 2024 evaluation boundary. Nothing is deficient.

## Provenance and local exports

Provenance includes the source 15m dataset and normalized-candle fingerprints,
aggregation version, policy fingerprint, symbol/timeframe, fixed execution
resolution, UTC bucket rule, evaluation interval, warmup interval/component
counts, and normalized aggregate fingerprint. The aggregate dataset fingerprint
hashes this provenance before adding its own hash field. Data timestamps and
requested interval boundaries are necessary identity inputs; wall-clock or
retrieval timestamps and local filesystem paths are absent.

Changing future source data may change whole-view provenance and fingerprint,
while the earlier visible-window fingerprint remains unchanged. No future data
is passed into that visible window.

Nine exclusive JSON exports and a summary were created under
`artifacts/research-data/development-v1/`, outside baseline-001. Each export is
named `{symbol}-{timeframe}.json` and contains provenance plus 99 warmup and all
evaluation candles. `summary.json` records counts and every view fingerprint.
This directory is Git-ignored. Exporting was an explicit data-only operation;
the library does not write on import, build or load. Existing output files were
not overwritten. No reload API or strategy evaluation is added here.

The baseline report, checkpoint, diagnostic report, acquisition manifest and
all used historical CSV bytes matched their pre-operation hashes afterward.
No additional source acquisition occurred, no 2026 data was accessed, and no
Phase 6B3/6B4 work was implemented.
