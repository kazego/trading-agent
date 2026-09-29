# LIVE-0B Activation-Prep A1

FORWARD_PAPER / NON_EXECUTING. No activation or final symbol snapshot is supplied.

`paper-symbol-rules-v2` adds immutable canonical raw provenance and explicit
Decimal-string price bounds to RuleSnapshot. Legacy v1 records, IDs, decoding
and financial behavior remain unchanged. Shared historical SymbolRules and the
historical simulation engine are untouched. Raw strings, including trailing
zeros, remain in provenance; normalized financial rules retain their existing
canonical serialization. Acquisition time is provenance, never a runtime clock
input. The complete provenance is bound into the snapshot ID.

Required static filters: PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL.
Quantity steps must match with compatible zero-origin grids; bounds intersect
using maximum minimum and minimum maximum. No precision-field substitution.
MIN_NOTIONAL retains the frozen modeled slipped-entry-price calculation; it is
not a claim to reproduce Binance's mark-price-based market-order validation.

MAX_NUM_ORDERS is NOT_APPLICABLE_NON_EXECUTING. PERCENT_PRICE is
DYNAMIC_EXCHANGE_RULE_OUTSIDE_FROZEN_MODEL: retained, never enforced using a
substitute mark price. POSITION_RISK_CONTROL is OUTSIDE_PAPER_SCOPE_UNDOCUMENTED:
its exact string is retained without interpreting NONE or any other string.
This explicit scope exclusion is permitted only because no Binance orders or
account positions exist. Its semantics are a blocker before real execution.
Unknown filters, fields, classifications, duplicates and missing filters fail
closed. No network acquisition is added. Response checksum is recorded provenance;
the caller must separately verify the original response when preparing a real
snapshot. A1 does not attest an arbitrary caller-supplied response hash.

V2 checks actual slipped entry and already-rounded initial ATR stop/target
against enabled inclusive price bounds before sizing/opening. Zero disables the
respective bound; tick must remain positive. Unsupported grid origins fail.
Failure is REJECTED_EXECUTION / STATIC_PRICE_BOUNDS. No clamping, extra fill
rounding, resizing or fallback is allowed. Later gap exits are untouched.

This is a versioned compatibility guard with new out-of-bounds rejection
behavior, not a claim of universal behavioral equivalence. Strategy, ATR,
slippage, fees, sizing and B2 arbitration are unchanged. Current integrated
B3/B4/B5 source identities change; historical committed identities remain
reproducible from their recorded Git revisions. No historical artifact migrates.

Synthetic offline tests cover bounds, malformed input, exact raw provenance,
legacy roundtrip, opaque exclusions, no fallback, sizing/fills and gap exits.
The B3 import-isolation allowlist adds only datetime for UTC provenance parsing.
No dependencies, scheduler, LIVE-0A files or runtime evidence are changed.
