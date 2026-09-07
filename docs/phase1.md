# Trading Agent v0.2 — Phase 1

Baseline: `6c2bceb11bf2822219537d9fb4044c871f658cf3` on `master`.
Review date: 2026-09-07.

Phase 1 adds offline strategy regression tests and this operating/API contract.
It does not make the existing bot v0.2-safe. Application code and dependency
requirements remain unchanged. No Binance API connection, credentials, SDK
installation, or order submission was used for this phase.

## Agreed operating contract

- Binance Futures Testnet ONLY; dedicated account; USDT-M; One-Way mode only.
- Fixed watchlist: **BTCUSDT, ETHUSDT, ADAUSDT**. This supersedes the earlier
  single-symbol audit assumption. No dynamic symbol discovery or other symbols.
- Maximum **one open position across the entire account**, not one per symbol.
  Pending/unknown entry intents must reserve that capacity in later phases.
  Unexpected exposure outside the watchlist blocks entries; it must not be
  ignored merely because the strategy does not evaluate that symbol.
- Each watchlist symbol uses its own candles, exchange filters, tick/step sizes,
  quantity/notional limits, and precision metadata. Never reuse BTC rules for
  ETH or ADA. Reading exchange information does not expand the watchlist.
- Preserve EMA 9/21, `adjust=False`, `get_signal(df)`, LONG/SHORT/NONE, and the
  100-candle/forming-candle convention. The current timeframe is 15 minutes.
- Strategy never places orders; every trade goes through risk authorization.
- Both valid SL **and** TP are mandatory for live exposure.
- Binance is the authority for live positions and orders.
- `TRADING_ENABLED` must default to `False` when introduced. Disabling entries
  must never disable protection, emergency closing, reconciliation, or other
  risk-reducing actions. Entry risk limits must not prevent emergency exits.
- At most one entry intent per strategy version/symbol/timeframe/signal candle,
  including across restarts. Repeated signals may still be returned by strategy;
  intent deduplication belongs outside it.
- Unknown/inconsistent state blocks new entries.
- Report an emergency close successful only after Binance confirms flatness.
- No real-money support. Original README live-trading instructions describe the
  baseline and are not part of the approved v0.2 operating contract.

These are requirements for later phases, not newly implemented safeguards.
The baseline still uses `config.SYMBOL = "BTCUSDT"`, lacks `TRADING_ENABLED`,
and does not enforce the full account contract. Do not run the bot to run tests.

Before later execution work, settle margin mode, simultaneous-signal priority,
management polling deadlines, and exact loss-limit/reset policies. They are not
silently decided or implemented by Phase 1. Selection among simultaneous signals
must preserve the single account-wide entry slot and each symbol's EMA output.

## Offline regression suite

From the repository root, with Python and the existing pandas dependency:

```text
python -B -m unittest discover -s tests -v
```

The runner is standard-library `unittest`; no pytest or Binance SDK is required.
Tests import only the local strategy/config and pandas, use synthetic data,
and do not import the bot or client. They require no credentials or network.
`-B` avoids creating bytecode files.

Validated locally with Python **3.12.14**, pandas **3.0.1**, using the bundled
Python runtime because Python was not on PATH. No packages were installed.
Result: **15 tests passed**. Pandas remains unpinned in the original requirements;
other versions are not certified by this run.

Behavior frozen:

1. EMA spans are 9 and 21. Hand-calculated indicator values distinguish the
   original recursive (`adjust=False`) calculation from normalized weighting.
2. A LONG requires previous fast <= slow and current fast > slow; SHORT uses
   previous fast >= slow and current fast < slow. Equality on the previous row
   permits a cross; equality on the current row is NONE.
3. No crossover, including persistent up/down trends and equal EMAs, is NONE.
4. With a valid `close` column, lengths 0 through 22 return NONE. Exactly 23 rows
   can produce a signal. This is the existing minimum, not a new warm-up policy.
5. On a 100-row oldest-first frame, compare positions 97 and 98 (`-3`/`-2`).
   Ignore a crossover appearing only on the forming row 99 (`-1`). An older
   crossover is not repeated once both compared rows are on the same side.
6. Changing the forming close cannot change the signal on the closed rows.
   Candle selection is positional even with a datetime index.
7. `get_signal` preserves values, columns, dtypes, index, and attributes,
   including preexisting indicator columns and insufficient-history input.
   `add_indicators` returns a separate frame and preserves original columns.

Most tests use the real EMA calculation. One comparison-table test substitutes
indicator values to test exact equality independently of floating-point EMA
generation. No strategy implementation is mocked in the end-to-end fixtures.

The suite does not claim freshness/NaN validation, profitability, backtesting,
watchlist scheduling, or order safety. It freezes the current valid-input
strategy behavior. The bot's 100-row fetch remains unchanged; tests exercise
100-row fixtures without enforcing that length inside `get_signal`.

## SDK/API compatibility investigation

Evidence is static: the upstream **v1.0.37 tag**, compared with current official
Binance documentation. The SDK is not installed in the test runtime, so this is
not a package-artifact verification or a Testnet certification.

### SDK routes confirmed in tagged source

All routes below are relative to `/fapi/v1/`.

| Operation | python-binance 1.0.37 method | Route / identifier |
| --- | --- | --- |
| Normal order | `futures_create_order` | POST `order`; `newClientOrderId` |
| Conditional SL/TP | `futures_create_algo_order` | POST `algoOrder`; `clientAlgoId` |
| Normal lookup | `futures_get_order` | GET `order`; `origClientOrderId` |
| Conditional lookup | `futures_get_algo_order` | GET `algoOrder`; `algoId` / `clientAlgoId` |
| Open conditional orders | `futures_get_open_algo_orders` | GET `openAlgoOrders` |
| Individual cancellation | `futures_cancel_order` / `futures_cancel_algo_order` | DELETE `order` / `algoOrder` |
| Bulk cancellation | `futures_cancel_all_open_orders` / `futures_cancel_all_algo_open_orders` | DELETE `allOpenOrders` / `algoOpenOrders` |
| Symbol filters | `futures_exchange_info` | GET `exchangeInfo` |

`futures_create_order` automatically routes STOP_MARKET/TAKE_PROFIT_MARKET to
algo orders, changes `stopPrice` to `triggerPrice`, and drops `newClientOrderId`:
provide `clientAlgoId` explicitly. Open-order queries/cancellation default to
ordinary orders; conditional helpers or `conditional=True` are separate.
Normal order parameters, including `reduceOnly`, pass through to the request.
The constructor pings by default.

Source: [tagged SDK client](https://github.com/sammchardy/python-binance/blob/v1.0.37/binance/client.py),
particularly `futures_create_order`, `futures_get_order`, `futures_get_open_orders`,
the cancellation/algo methods, and `futures_exchange_info`.

### Testnet endpoint discrepancy

The tagged base client selects `https://testnet.binancefuture.com/fapi` for
`testnet=True`; `demo=True` selects `https://demo-fapi.binance.com/fapi`.
If both flags are true, `testnet` takes precedence. Current Binance documentation
lists `https://demo-fapi.binance.com` as the Testnet REST base.
This discrepancy is not proof the older hostname fails, nor proof it remains
compatible. Endpoint selection must be explicitly resolved before integration;
never fall back to production. Phase 1 changes no flags or URLs.

Sources: [tagged SDK base client](https://github.com/sammchardy/python-binance/blob/v1.0.37/binance/base_client.py),
[Binance Testnet/general information](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info).

### API semantics and current application gaps

Binance documents separate standard/algo endpoints. Algo responses use fields
including `algoId`, `algoStatus`, `orderType`, and `actualOrderId`; they require
normalization, not ordinary-order parsing. One-Way market closes can use
`reduceOnly`; conditional `closePosition=true` excludes both quantity and
reduceOnly. Source: [Binance trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).

Exchange information exposes symbol-specific filters. Decimal precision alone
does not validate tick/step increments, quantity bounds, or minimum notional.
Source: [Binance market-data API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

Repository conclusions (static inference, not live observation):

- The prior audit's uncertainty about SL/TP submission routing is resolved at
  source level. A dependency upgrade is not justified solely to obtain algo
  methods: the pinned version already exposes them.
- `has_protective_orders()` reads only ordinary orders. Thus the application can
  fail to find protections it submitted through automatic algo routing.
- Its cancellation wrapper also covers only ordinary orders and is not called
  by the loop. Later cleanup must cover both order families explicitly.
- Simply switching the query will not fix its `any()` check: both valid SL and
  TP must be matched to current exposure using normalized response fields.
- The application does not assign durable IDs or confirm fills/flatness. Its
  emergency close omits reduce-only semantics. SDK capability is not application
  safety; these remain execution/reconciliation work for later phases.
- The current client uses precision metadata rather than filters and has one
  global symbol; later watchlist work must use symbol-specific state.

### Unresolved Testnet contract checks — later, not run in Phase 1

Before enabling entries, verify the following on the dedicated Testnet account
with the exact installed SDK artifact and explicit Testnet endpoint configuration:

1. **Endpoint/authentication:** selected hostname, account credentials, signing,
   time synchronization, and no production route; resolve legacy versus demo
   hostname behavior. Verify actual One-Way/USDT-M account configuration.
2. **Watchlist:** availability and trading status of BTCUSDT, ETHUSDT, ADAUSDT;
   symbol-specific exchange filters and 100-candle responses, with the final
   forming candle. Never substitute another symbol if one is unavailable.
3. **Normal orders:** response/fill quantities and prices, stable assigned client
   ID, and lookup by client ID after an uncertain response and after completion.
4. **Both protections:** accepted SL/TP parameters and serialized booleans;
   both discoverable via open algo orders; correct side, coverage, trigger basis,
   and IDs. Verify triggered algo-to-actual-order linkage and terminal states.
5. **Cancellation:** individual lookup/cancel by both identity forms; ordinary
   versus algo cleanup; cancellation racing a trigger/fill; survivor cleanup.
   Confirm state after cancellation rather than trusting acknowledgement alone.
6. **Safe close:** reduce-only behavior for long and short exposure, partial
   close, already-flat and concurrent protection-trigger cases. Confirm the
   account is flat and no opposite position was created before reporting success.
7. **Failure outcomes:** accepted-but-response-lost submissions, duplicate IDs,
   delayed lookup visibility, and expired history. Combine offline fault
   injection with Testnet observations; do not blindly retry an unknown order.
8. **Account-wide invariant:** position/order snapshots cover all symbols;
   simultaneous watchlist signals cannot result in two entries. This will need
   execution/recovery tests after those components exist.

Checks involving fills, protection, persistence, and restart require later
implementation. A mock or static route inspection cannot establish server-side
acceptance, fill safety, endpoint availability, or live recovery correctness.

## Phase boundary

No persistence, execution engine, risk changes, watchlist implementation,
runtime entry switch, Docker, AI, backtesting, dashboard, Telegram, or VPS work
is included. Those requirements are documented here only. No commit is made.
