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
