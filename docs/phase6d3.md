# Phase 6D3 — frozen ATR protection

Implemented only Strategy C's protection policy. The entry is the existing,
explicitly selected 4h EMA9/21 research signal, without a ribbon or trend filter.
No saved DEVELOPMENT experiment, diagnostic D, runner integration, registry
record, validation or holdout access is included. No trailing stops are provided.

## Authorization and execution boundary

`research.protection.ATRProtection(feature_view, snapshot, schedule, event)`
requires an authorized Phase 6D1 FeatureView and FeatureSnapshot and an explicit
existing ResearchSchedule using EMA9/21. Construction checks symbol, 4h boundary,
positive finite Decimal ATR, the complete feature content/fingerprint against
Phase 6D1, and equality of the last 99 completed candles with the EMA schedule.
The existing schedule verifies the execution dataset and known opening price.
An actionable LONG or SHORT is required. Split checks remain DEVELOPMENT-only;
HISTORY_ONLY initializes features but cannot be an entry boundary.

Construction happens before execution and freezes the ATR and accepted decision
binding. The policy retains no candles/view. Execution never recalculates ATR.
A different decision, timestamp, source, direction or opening price is rejected.
As elsewhere, Python introspection or deliberate mocking is not a security boundary.

Select the policy explicitly with the keyword-only argument:
`open_position(..., protection_policy=issued_atr_policy)`.
Omitting it (or passing None) uses the existing fixed 1.5%/3% path. No runner or
portfolio selects ATR implicitly. Fixed Position fields/serialization are unchanged.
ATRPosition extends Position only for the explicit research path. The same fill,
fee, process_bar and close_position functions handle both kinds of position.
The small policy seam validates the binding, supplies rounded levels and attaches
an immutable risk record; it does not implement another execution engine.

## Prices and Decimal rounding

For actual adversely slipped entry fill E and frozen ATR14 A:

- LONG: raw stop E - 2A; raw target E + 4A.
- SHORT: raw stop E + 2A; raw target E - 4A.

All arithmetic uses the existing fixed 50-digit Decimal context. For tick t,
rounding is `(price/t).to_integral_value(ROUND_FLOOR or ROUND_CEILING)*t`.
LONG stop uses FLOOR and target CEILING; SHORT stop uses CEILING and target FLOOR.
Exact ticks remain exact. Both prices must be positive and strictly on the correct
side of entry after rounding; collapsed/unrepresentable levels reject the entry.
No later candle, ATR change or forming-bar component moves these stored prices.

## Planned risk and limits

C requires risk_fraction = 0.01 and the frozen maximum leverage setting 3.
Requested price risk R = current realized equity * 0.01.
Distance d = abs(actual entry fill - rounded stop).
Raw risk quantity = R/d. It does not use the old percentage stop, target distance,
unrounded 2A, or pre-slippage opening price.

The shared sizing function retains its original fee-inclusive risk basis by
default. Only the explicit C path selects `price_distance_risk=True`. It reuses
margin quantity `equity/(entry/3 + entry*entry_fee_rate)`, maximum quantity,
quantity-step FLOOR, minimum quantity and minimum notional checks. Research rules
must already contain the approved effective LOT_SIZE/MARKET_LOT_SIZE intersection;
no exchange values are invented or obtained here. SymbolRules validates required
positive ticks/steps/minimums and bounded quantities. This model has no additional
exchange price-range fields; its existing tick/positive-price contract is retained.

Final quantity q is the step-rounded-down minimum of risk quantity, margin quantity
and maximum quantity. Actual planned price risk = q*d must be <= R; otherwise
entry fails closed. Risk utilization = actual/R. There is no allowance for an
exchange minimum to increase risk. A minimum/filter failure rejects the entry.

Independent examples at equity 1000, E=100, budget 10, before binding caps:

| ATR | Rounded LONG stop | Quantity | Planned price risk |
| --- | --- | --- | --- |
| 1 | 98 | 5 | 10 |
| 2.5 | 95 | 2 | 10 |
| 0.01 | 99.98 | 30 (3x cap) | 0.60 |

With ATR=1 and tick=0.3, LONG stop is 97.8; quantity at step .001 is 4.545,
so planned price risk is 9.999. Wider rounded stops lower risk-sized quantity.

These are planned *price-distance* risk figures. Unlike the unchanged fixed
baseline's planned_stop_risk (which includes modeled costs), C's field is price
risk. Entry/exit fees and adverse exit slippage still apply to realized P&L and
can make realized loss exceed this figure; stop gaps can do so too. Entry fees
remain reserved alongside initial margin. No claim of a guaranteed 1% realized
loss is made. Funding and liquidation remain excluded. Later reporting integration
must preserve this explicit difference in risk basis.

## Identity and tests

ATRPosition carries ATRRiskRecord: family/version `WILDER_ATR_PROTECTION` /
`atr14-2x-4x-v1`, period 14, multiples 2/4, ATR, feature fingerprint, decision ID,
symbol/time, fill, rounded stop/target, quantity, requested/actual risk and
utilization, complete normalized rules fingerprint, config fingerprint and policy
source hash. Identity uses canonical Decimal strings and SHA-256; no wall-clock
or local paths participate. Source/software identity is also bound through the
existing feature and EMA decision identities. The record is included in the
position retained by shared ClosedTrade. Default fixed serialization has no new
fields.

Offline synthetic tests cover independent risk examples, both directions, tick
rounding, caps/filters, current equity, costs, immutable identity, authorization,
source/feature mismatches, no signal, invalid ATR/prices, future/forming mutation,
no execution-time ATR computation, dual touch, adverse gaps, favorable TP gaps,
EOD and fixed-default regression. Existing feature, A/B and baseline tests remain
unchanged. No market data is evaluated by these new tests.

Runner/spec/portfolio integration and any saved experiment execution remain
future work requiring the next phase; Phase 6D4 has not begun.
