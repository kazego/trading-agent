# Phase 6A: immutable baseline diagnostics

Run explicitly from the repository root:

```
python -m backtest.diagnostics --diagnose-existing-baseline
```

This reads baseline-001's existing report, trusted local checkpoint and validated
candles. It never calls a strategy, simulation, downloader or Binance endpoint.
It verifies the canonical baseline report fingerprint, checkpoint portfolio
fingerprint and normalized dataset fingerprints. All existing baseline file
bytes are hashed before and after calculations; writes are confined to a new
`artifacts/baseline-001/diagnostics/` directory. Existing diagnostics are refused.
The checkpoint is a locally generated pickle, not an externally supplied file.

Outputs are `report.json` and `report.txt`. Canonical diagnostic JSON uses Decimal
strings, sorted keys and a content fingerprint excluding its own fingerprint
field. Source code hash and input dataset fingerprints are recorded. There are
no wall-clock timestamps or random identifiers in the output. Diagnostics and
all original artifacts remain Git-ignored; no original report is regenerated.

## Definitions fixed before measurement

Calendar attribution uses UTC exit-candle opening dates, so trades crossing a
month/year are assigned to their exit month/year. This is realized attribution,
not a monthly MTM return. Zero-trade months are included. Monthly cumulative net
P&L is additive; group win rates and profit factors are not additive.

Duration is bounded: OPEN exits have exact boundary duration; CLOSE exits use
recorded candle end (+899999ms). INTRABAR duration is reported as exit-open minus
entry-open through that value plus 15 minutes. Statistics provide both bounds,
never an invented intrabar exit time. Percentiles use linear interpolation.

MFE/MAE use the actual slipped entry as reference and full OHLC strictly before
the exit candle. On OPEN/INTRABAR exits, only the known exit open and exit
reference price are included. Exit H/L are excluded because they could occur
after closing. CLOSE exits permit full exit OHLC. Thus INTRABAR excursions are
conservative observed lower bounds, not exact lifecycle extrema; threshold
counts undercount unknown exit-candle paths. No future prices feed execution.
R uses initial planned_stop_risk, which includes modeled costs, as denominator;
USDT excursions are price-only excursion times original quantity. Thresholds:
meaningful MFE >= half the initial entry-to-stop price distance; near-target MFE
>=90% of entry-to-target distance; large MAE >= half the initial stop distance.

Whipsaw patterns are descriptive and can overlap across definitions:

- Maximal consecutive STOP_LOSS runs of at least two actual portfolio trades.
- Opposite direction in consecutive same-symbol trades, with next entry within
  four boundaries after the previous exit boundary; P&L counts the later trade.
- Three alternating same-symbol entries within 16 boundaries; overlapping
  windows counted, but P&L uses the union of their members once.
- UTC days with at least three net-losing exits; only those losing trades enter
  cluster P&L.

Occupancy measures positions already open at evaluation boundaries. Entry-candle
exposure is not included in this boundary count. Blocked signals are availability
counts, not counterfactual trades or missed profits.

Worst/best tails use net P&L, deterministic decision-ID tie breaks, and ceil(5%
trade count). Signed contributions divide by total net P&L, so profitable tails
have negative contributions to a net loss. Tail groups overlap and must not be
summed. No market regimes are inferred without measurements.

Gross P&L already includes slippage. Exact per-fill price/reference differences
at original quantities give recorded slippage impact; adding it to gross is a
fixed-fill reference attribution, not a zero-cost strategy. Costs affected
sizing, protections and accepted trades, so a counterfactual portfolio cannot be
recovered by subtraction. Funding/liquidation remain excluded. No parameters,
strategies, costs or portfolio limits are changed or tested in Phase 6A.
