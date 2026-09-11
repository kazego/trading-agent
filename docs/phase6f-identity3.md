# Semantic Contract V2

Identity2 fingerprints a residual module AST, including constructors, so even
an unused optional parameter is classified as a change. It remains immutable
historical evidence. No Identity2 implementation or fingerprint is rewritten.

V2 separates three things:

1. Historical provenance: unchanged read-only six-run/selection verification,
   with Identity2 derived and checked against its original Git source snapshot.
2. Behavioral contract: explicit specification plus frozen canonical synthetic
   component observations in `research/semantic_contract_v2.json`.
3. Implementation approval: a separate complete explicit source-file fingerprint.

The spec binds EMA9/21 comparisons, 99 completed plus forming-open-only rows,
600 completed feature history, Wilder ATR14 initialization/recurrence, 2x/4x
protections, 1% rounded-stop risk, leverage cap, symbol priority/one position,
4h/15m boundaries, adverse costs/slippage, ordering and aggregation requirements.
Golden vectors capture EMA values/crossover equality cases, variable true-range
ATR, forming isolation, history rejection, boundary handling, OHLCV aggregation,
invalid ordering, rounded ATR levels, sizing/margin caps and fee configuration.
They invoke small component functions, not simulation/portfolio runners.

Vectors were frozen only after historical verification and confirming that the
current semantic-source file hashes matched the selected historical snapshot.
These are new synthetic observations, not recalculated historical results.
Fixture revision changes the V2 contract fingerprint and requires explicit review.
Missing specification/fixture components fail closed.

`verify` requires both an expected contract fingerprint and a separately approved
implementation fingerprint. It compares actual observations to frozen vectors.
An unused constructor parameter does not change the contract; its implementation
fingerprint still changes and needs separate source review. Renaming a required
constructor without preserving its interface does not silently pass.

Finite golden vectors are regression evidence, not proof of mathematical
equivalence for arbitrary programs. They cannot automatically approve unseen
implementation changes. The full source approval gate rejects those changes
even if they happen to match all vectors. Portfolio rules and other paths not
executed by these small probes remain bound by the specification and full
implementation identity; they are not claimed comprehensively fixture-tested.
No source scope is broadly excluded to make a refactor pass.

The read-only migration binds Identity2, historical selection/result, V2 and the
approved current implementation. Future validation must preserve that chain
and explicitly approve its infrastructure revision. This identity module does
not grant execution or market-data authorization. Runtime generalization is
not implemented here. Existing gates, files and numerical behavior are unchanged.

No simulation/backtest, validation attempt or real market-data access occurs.
Component probes use synthetic 2024 timestamps only. TRUE_HOLDOUT remains
unauthorized and validation has not begun.
