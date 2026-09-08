# Phase 4B: normalized read-only Futures Demo adapter

Baseline: `ff210e50f63eb38702f9ac1b30c17b60e14396b4` on master.
Phase 4A's endpoint decision is retained. This phase performs no live verification.

## Scope and legacy compatibility

`BinanceFuturesClient` is now a read-only adapter. Its old order-submission,
protection-checking, cancellation, closing, and startup leverage-change behavior
has been removed. No exchange mutation is exposed by the adapter.

The original `bot.py` is intentionally not integrated with this new API: it still
expects the legacy methods and will fail during its startup adapter lookup. Do not
use it as a runnable trading entry point. Execution integration belongs to a later
approved phase. `strategy.py`, its EMA/candle conventions, `risk_manager.py`, config,
persistence, and logging implementation are unchanged. No runtime entry-switch
integration, execution state machine, or reconciliation is included.

## Demo construction and transport boundary

The default factory is the pinned python-binance 1.0.37 `Client`. Construction uses
`demo=True`, `testnet=False`, `ping=False`, `verbose=False`, a positive configured
read timeout, and `allow_redirects=False`. It does not set leverage or query Binance.
`USE_TESTNET` must remain exactly True as the application policy; SDK `testnet=False`
selects the newer Demo service and does not enable production.

The resolved Futures URI for each version (1/2/3) must match
`https://demo-fapi.binance.com/fapi/v{version}/time`. Validation runs on construction
and before every read. Wrong flags, the old testnet hostname, production, or another
URL fail with ConfigurationError. No fallback or automatic redirect is allowed.
The injected SDK factory is an offline testing seam, not a configuration for hosts.

The SDK's Demo route selection and ping behavior are verified statically in the
[pinned base client](https://github.com/sammchardy/python-binance/blob/v1.0.37/binance/base_client.py)
and [pinned client](https://github.com/sammchardy/python-binance/blob/v1.0.37/binance/client.py).
Binance documents the Demo hostname in its
[general information](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info).
This is not evidence of live service availability for a particular account.

## Read API

All transport operations below are GET requests. Except time and exchangeInfo,
they require signed Futures Demo credentials. The adapter never accepts arbitrary
endpoint/method names or raw SDK parameter dictionaries through its public methods.

| Adapter method | SDK method | REST path |
| --- | --- | --- |
| get_server_time | futures_time | /fapi/v1/time |
| get_exchange_info | futures_exchange_info | /fapi/v1/exchangeInfo |
| get_account_info | futures_account | /fapi/v2/account |
| get_balances | futures_account_balance | /fapi/v3/balance |
| get_position_mode | futures_get_position_mode | /fapi/v1/positionSide/dual |
| get_positions | futures_position_information | /fapi/v3/positionRisk |
| get_open_ordinary_orders | futures_get_open_orders | /fapi/v1/openOrders |
| get_ordinary_order | futures_get_order | /fapi/v1/order |
| get_open_algo_orders | futures_get_open_algo_orders | /fapi/v1/openAlgoOrders |
| get_algo_order | futures_get_algo_order | /fapi/v1/algoOrder |
| get_recent_fills | futures_account_trades | /fapi/v1/userTrades |

Ordinary lookup takes a watchlist symbol and exactly one `order_id` or
`client_order_id`. Algo lookup takes a watchlist symbol and exactly one `algo_id`
or `client_algo_id`; the symbol is checked against the response, while only the
identity is sent per the current documented algo query parameters. ID/symbol
mismatches fail closed. Source: [official trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

Fill queries take a watchlist symbol, limit 1..1000 (default 500), and optional
`from_id`. The result is one bounded page. `limit_reached` warns of possible further
results; even a shorter page does not establish complete historical accounting.
No automatic pagination or retries are implemented.

## Immutable models

`models.py` adds frozen dataclasses, with immutable tuples for collections:

- `ExchangeSnapshot`: endpoint and local request/receipt epoch-millisecond times.
- `SymbolFilters` / `ExchangeInfoSnapshot`: the three configured symbols' rules.
- `AccountSnapshot`: account flags and wallet, margin, available, unrealized totals.
- `BalanceSnapshot` / `BalancesSnapshot`: per-asset wallet, available, cross-wallet,
  cross-unrealized values, and exchange update time; USDT must be present.
- `PositionSnapshot` / `PositionsSnapshot`: all returned rows, inferred absence,
  exposure symbols, and explicit inconsistency markers.
- `OrdinaryOrderSnapshot` / `OrdinaryOrdersSnapshot` and
  `AlgoOrderSnapshot` / `AlgoOrdersSnapshot`: separate identities and semantics.
- `FillSnapshot` / `FillsSnapshot`: trade/order identity, side, price, quantity,
  realized P&L, commission/asset, time, and bounded-page metadata.

Financial strings are parsed directly into finite Decimal values. Transport floats,
booleans, NaN, infinity, malformed numeric strings, and disallowed negative values
fail closed. Signed balance/P&L/commission fields retain their signs. Numeric exchange
IDs become canonical positive decimal strings; client IDs remain nonempty strings.
No exchange status is mapped to a local persistence/execution status in this phase.

Metadata records observation time, not exchange-wide atomicity or freshness
certification. Individual order lookups return immutable row models. Collections
carry request/receipt metadata. Models have no readiness or trading authorization.

## Filters

Each watchlist symbol must be a USDT-quoted, USDT-margined perpetual. Missing symbols,
duplicate symbols/filter types, required-field omissions, invalid numeric fields,
unknown symbol status, inverted bounds, and unusable quantity steps fail closed.

Required PRICE_FILTER, LOT_SIZE, and MARKET_LOT_SIZE are parsed independently.
The model retains tick and price bounds; separate ordinary/market step, min/max
quantity; precision fields; optional MIN_NOTIONAL and PERCENT_PRICE multipliers.
No values are copied between symbols or substituted from precision.

Zero PRICE_FILTER values retain Binance's disabled-rule meaning. Quantity steps
must be positive: an unexpected zero requires contract review, not guessed rounding.
Missing MIN_NOTIONAL/PERCENT_PRICE produces None. An alternate NOTIONAL filter
is explicitly unsupported. Other filter types (such as order-count limits) are
outside this selected model; it is not a complete order-validation engine.
See [official filter definitions](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/common-definition).

## Position semantics

`get_positions()` performs one unfiltered account-wide V3 positionRisk read. Binance
specifies that V3 omits symbols without positions or open orders. Only after a
successful list-shaped response whose every row validates are missing watchlist
symbols represented as quantity Decimal('0'), `inferred_flat=True`, with price,
position side, and update-time fields left None. An error, object envelope, duplicate
row, invalid side, or malformed amount never becomes an empty/flat snapshot.

Returned rows outside the watchlist are retained so unexpected exposure is visible.
`issues` can include HEDGE_STYLE_ROWS, MULTIPLE_EXPOSURES,
OUTSIDE_WATCHLIST_EXPOSURE, and UNEXPECTED_MARGIN_ASSET. Both hedge rows remain
visible; positions are never netted across sides. `flat` is false when an issue
exists. These issues require later entry blocking; this adapter performs no action.

`get_position_mode()` independently requires One-Way and raises AccountModeError
for Hedge mode. A flat position snapshot alone does not prove One-Way configuration.
An empty successful V3 result describes absent exposure at that observation, not
stable account readiness. Later coordination must check mode, all observations,
freshness, protections, and risk. Reads across endpoints are not an atomic snapshot.

## Ordinary versus algo orders

Both open-order families are queried without a symbol filter and retain all symbols.
Empty arrays are valid. Object/error envelopes and duplicate exchange identities
within `(symbol, family)` are rejected. Equal numeric IDs in different families
remain distinct. Unknown status/type/side/working-type values fail closed.

Ordinary rows preserve order_id, client_order_id, side, type, status, quantity,
executed quantity, price, optional stop price, reduce-only, close-position, position
side, and optional working type. Executed quantity cannot exceed requested quantity.

Algo rows preserve algo_id, client_algo_id, algo_status, conditional type, trigger,
optional quantity, reduce-only, close-position, position side, working type, optional
actual_order_id, and optional actual quantity. Absent quantity is allowed only for
close-position orders; an explicitly returned zero quantity is retained. Empty
actualOrderId becomes None. No ordinary ID is substituted for an algo identity.
Missing optional values remain unknown. These observations do not certify valid
SL/TP coverage or successful execution.

## Error mapping

- Requests timeout / TimeoutError / Binance -1007: ReadTimeoutError.
- Requests connection failure / -1001 / -1008 / HTTP 5xx: TransientReadError.
- -1003 / HTTP 418 or 429: RateLimitError.
- -2014 / -2015 / -1022 / HTTP 401 or 403: AuthenticationError.
- Wrong environment/configuration: ConfigurationError.
- Authoritative Hedge position mode: AccountModeError.
- Missing orders (-2013), clock/signing-window errors (-1021), malformed responses,
  unsupported values, and otherwise unknown exceptions: UnknownExchangeStateError.

All errors block entries under the existing classifier. Recoverable reads are not
retried automatically. No raw exception, response body, request URL, or credentials
are logged by the adapter. Translated exceptions suppress the original exception
context. HTTP redirects are disabled; there is no production retry/fallback.

## Offline evidence and remaining live checks

The unit tests use fake SDK and requests modules. This permits the full suite to
run without installing the SDK or its dependencies in the bundled Python runtime.
No credentials, Binance reads, mutations, or dependency changes were used. These
tests verify our boundary and parsing, not actual SDK/server interoperability.

Before an explicitly authorized live READ-ONLY session:

1. Confirm the operator can create Futures Demo credentials through Demo API
   Management. The previously reported Futures web 404 is still unresolved.
   [Official creation instructions](https://www.binance.com/en/support/faq/detail/ab78f9a1b8824cf0a106b4229c76496d)
   do not prove account-specific access.
2. Verify the exact installed 1.0.37 artifact, URL resolution, redirect refusal,
   signing, timestamp tolerance, and credential permissions without exposing secrets.
3. Exercise all eleven read methods; record sanitized response shapes and schema
   variations, especially algo statuses/IDs and ordinary versus algo visibility.
4. Check actual BTC/ETH/ADA trading status, increments, both lot filters, notional,
   price bounds, and optional fields. Unexpected metadata must block, not default.
5. Verify One-Way configuration without changing it; compare known existing exposure
   and empty V3 responses with account observations, including omitted flat symbols.
6. Using already existing Demo records, verify each lookup identity form, algo-to-
   actual-order linkage, and account-fill pagination/history availability. Empty
   lists alone cannot prove normalization of nonempty records. No orders may be
   created merely to populate fixtures under this phase's authorization.
7. Verify wallet/margin/available semantics against actual account asset mode.
   Review read rate limits before setting any later polling schedule.

No optional live script was added. Do not run `bot.py` for these checks. Future
execution/reconciliation, SL/TP validity, closes, accounting, streaming, and 24/7
operation remain outside Phase 4B. No commit is made and Phase 5 is not started.

Verification result: 139 tests passed (the existing 98 plus 41 new adapter tests),
in 1.205 seconds on the final behavioral test run. Command:
`python -B -m unittest discover -s tests -v`.
