# LIVE-0A — FORWARD_PAPER / NON_EXECUTING

Implementation only. No real market data has been fetched, no run has been
armed, and no prospective start has been selected. This phase neither executes
trades nor starts Phase 6F validation. Historical research files and gates remain
unchanged. No credentials or new dependencies are needed.

## Scope and boundary

`forward.public_feed` uses Python's HTTPS transport with a literal
`fapi.binance.com` host, TLS certificate verification and only two GET routes:
`/fapi/v1/time` and `/fapi/v1/klines`. There is no SDK, configurable host, credential
loader, authentication header, signature, private/account route, trading route,
proxy configuration, redirect following or endpoint fallback. Public production
market data is intentional; this is not a Demo trading endpoint substitution.
See the [official USD-M market-data documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

Requests are explicit and bounded: ten-second timeout, two-million-byte response
limit, at most 1,000 closed 15m candles per request, ten pages per symbol per
invocation. Only BTCUSDT, ETHUSDT and ADAUSDT are accepted. A partial page, unknown
response, incorrect timestamp, float price, bad OHLCV, gap or redirect fails closed.
Errors never echo arbitrary response bodies or exception text. Import and `--help`
do not fetch anything. Pytest/unittest discovery never invokes the live collector.

This is a source/API boundary, not an OS network sandbox. The supplied code has
no order capability; an administrator or arbitrary replacement Python code could
use the network differently. The separately proposed externally enforced GET-only
gateway and network-disabled engine are not deployed by LIVE-0A. Do not describe
this local utility as a sandbox against a compromised interpreter.

## Frozen candidate and reuse

The selected candidate is `phase6d-c-atr-v1`, selection fingerprint
`92ee399eccbb27b0b6cfa32efbcfea1e9e837569bf61befd6d777eff10014d84`.
Semantic Contract V2 remains
`b9db00e5e2ec6fcf6cc34cde7c2144a3bdba73556c8d2bb05c7c7450cf68c27d`.

The observer reuses `backtest.data.Candle`, `research.data.aggregate_bucket`,
`exact_sum`, the frozen `research.features._atr` calculation, canonical encoding
and the Windows-tested `runtime.DatabaseLock`. It does not import the historical
strategy adapters, which enforce 2024 and transitively load trading config.
Those authorization guards are not bypassed or broadened. Instead, a small
forward-only EMA adapter is explicitly equivalence-tested against `get_signal`.
No shared runtime file or historical source identity is changed.

At UTC 4h boundary T, exactly 600 fully completed 4h bars are constructed from
9,600 contiguous 15m bars in `[T - 600*4h,T)`. Each aggregate has exactly sixteen
components: first open, maximum high, minimum low, final close, exact Decimal
volume sum. A completed candle requires close time strictly less than the
public server-time snapshot; incomplete buckets cannot be evaluated.

EMA9/21 uses only the last 99 completed closes, float64 pandas
`ewm(adjust=False)`, recalculated for each window. Its last two values are exactly
the frozen 100-row frame's `-3/-2` values because an appended forming row cannot
change earlier EWM values. No current opening price is needed for this signal.
Previous equality plus a strict current crossing is allowed; otherwise NONE.
Tests compare multiple synthetic forming prices and repeated rolling windows.

ATR14 reuses the last 100 completed 4h bars: 99 true ranges using the preceding
close; seed = mean of first 14 TRs; remaining recurrence = `(13*ATR + TR)/14`.
Decimal precision 50 is inherited from the frozen calculation. ATR is an
observation snapshot only. There is no entry or active trade at which to freeze it.

The immutable manifest records the future contract: 2 ATR stop, 4 ATR target,
frozen at entry, no trailing, 1% planned risk, max 3x leverage, BTC → ETH → ADA,
one simulated position total. These are declarative in LIVE-0A: no entry selection,
sizing, positions, protection prices, fills, exits, costs or P&L are implemented.
All three signals are retained in priority order; none is labeled accepted or
blocked by a nonexistent position. Reference fees/slippage remain research
assumptions (0.0005 and 0.0002 per fill); funding/liquidation are excluded.

## Descriptive context and observation identity

For each signal boundary and symbol, a separate snapshot stores the latest
fully completed UTC 1D, 1H and 15M bucket: OHLCV, return fraction, range fraction,
timestamps and component fingerprint. An unfinished day is never included.
The 4H signal/ATR snapshot supplies official candidate context. No new threshold,
trend filter, or feature weighting is selected.

Context is calculated after signals; it is not an argument to the signal function
and never affects the signal fingerprint. Tests replace all context outputs and
verify unchanged decisions. Stored raw candles and separate context permit later
joining of future outcomes, but LIVE-0A cannot label a signal successful/unsuccessful
without a separately authorized outcome methodology.

Signal fingerprints bind the candidate, contract, symbol, boundary, exact visible
history, EMA values and ATR. They exclude receipt time, paths and future candles.
Context has its own fingerprint. The observation envelope also records actual
receipt timing, so its audit digest intentionally differs if receipt times differ.
Implementation identity hashes an explicit list of runtime sources plus Python,
pandas and numpy versions; it is separate from the unchanged behavioral identity.
Every resume compares the frozen implementation. Changing source requires a new
review/activation, not silently adopting an old run under new code.

## T0 and evidence

An explicit operator activation freezes an exclusive manifest before any fetch.
T0 must be a UTC 4h boundary at least five minutes in the future; it has no default.
The initial required window is `[T0-600*4h,T0)` and is HISTORY_ONLY. T0 itself is
excluded from scoring: the first observation is **T0 + 4h**, following the request
that only boundaries strictly after T0 count. No pre-T0 decision is generated.

The manifest is never moved or overwritten, including after missed preparation.
At poll time public server time must agree with local time within 30 seconds and
cannot precede activation. The observer waits for all three symbols before issuing
a boundary. An observation within 120 seconds of T is labeled ONLINE; later
reconstruction is explicitly DELAYED_RECONSTRUCTED. Both refer to the prospective
calendar interval, but delayed reconstructions are not claims of signals issued
on time. Keep that distinction in later analysis. There is no achievable-fill
claim and no next-open paper execution in this phase.

Activation acknowledges that fetched warmup becomes observed history, and the
forward interval becomes prospective paper evidence. It cannot simultaneously
remain untouched TRUE_HOLDOUT. Nothing in this implementation edits the historical
research ledger or grants access through its validation/holdout paths.

## Durable state and restart

Artifacts reside exclusively under `artifacts/forward-paper/<run-id>/` (ignored):
`manifest.json` and `observer.sqlite`, with WAL/SHM and ownership-lock sidecars.
This is a separate schema and never opens a live or historical database.

- `identity`: one immutable manifest fingerprint.
- `candles`: `(symbol, open_ms)` primary key; canonical Decimal-string JSON,
  content digest, receipt time. HISTORY_ONLY membership is derived from manifest T0.
- `observations`: boundary primary key; all three signals, context, timing and
  digest. A boundary and its decision event are committed atomically.
- `events`: append-only labeled startup/shutdown, observation and BLOCKED records.

SQLite WAL with synchronous FULL and one OS owner protects local transactions.
UPDATE/DELETE triggers protect all four tables. Restart verifies SQLite integrity,
manifest binding, candle hashes/chronology and observation hashes/cursor continuity.
Cursors are derived from committed rows, never reset by initialization. Redelivered
identical candles are ignored; conflicting revisions reject the entire page.
Raw financial values are canonical strings, never SQLite REAL.

A failure during polling latches BLOCKED durably. Further polls refuse network
access. No automatic reset or broad retry is supplied, even for a transient public
failure: preserve the run for operator review. Missing data pauses observation;
there is no interpolation. Polling is bounded; a large backlog may need several
explicit invocations. Shutdown does not invent an end-of-data closure. There are
no simulated trades to log yet.

Hashes detect accidental corruption, not an attacker able to rewrite a database
and its hashes. Manifests are locally frozen evidence, not trusted third-party
timestamps. Network filesystems and concurrent writers outside the lock contract
are unsupported.

## Proposed activation — NOT RUN

After explicit operator approval, choose a future UTC 4h timestamp and a new ID:

```powershell
python -m forward arm --run-id live-0a-001 --t0 <YYYY-MM-DDTHH:00:00Z> --ack-prospective-exposure
```

This creates the local manifest only. The separate, explicitly authorized fetch is:

```powershell
python -m forward poll --run-id live-0a-001 --public-data-only
```

Each poll performs one bounded collection/observation pass. There is no automatic
startup, scheduled job or service. Repeated polling near 15m closes is an operator
responsibility for a later activation. All command output and decisions are labeled
**FORWARD_PAPER / NON_EXECUTING**. Do not use `bot.py`.

## Offline acceptance

`python -m unittest discover -s tests -p test_forward_observer.py -q` uses synthetic
2024 inputs and mocked HTTP only. It covers LONG/SHORT/NONE/equality equivalence,
rolling windows, ATR reuse, Decimal aggregation, completed context, future rejection,
gaps/duplicates, public-route restrictions, malformed responses, response/page bounds,
credential/config/SDK-free import, immutable activation, implementation mismatch,
global symbol barrier, delay disclosure, crash-safe cursors, redelivery/rollback,
ownership, append-only protection and corruption detection. No market-data fixture
is loaded. The full existing offline suite must also pass before activation review.
