# Phase 6F-Identity2

The second Phase 6F0 stop concerned whole-file candidate locks on
`research/features.py`, `research/strategy.py`, `research/schedule.py` and
`research/data.py`. Their numerical code and calendar guards share files.
This identity-only migration changes none of those files or their behavior.

`research.semantic_identity.migration_binding` is a new explicit path.
It first invokes the unchanged legacy historical verifier. Recorded whole-file
hashes, results, selection, plan, policy, manifests and ledger remain immutable.
Expected semantic source is derived from the verified Phase 6D Git snapshot,
not manually copied hash constants or today's source tree.

## Semantic scope

`COMPONENTS` explicitly lists every existing top-level function/class in each
mixed module. Missing, duplicate, renamed and additional components fail closed.
The semantic projection retains the complete AST: imports, globals, decorators,
signatures, function/class bodies and module-level statements. Formatting and
comments are not executable semantics; AST location metadata is excluded.

- Features: chronology and completed windows, typed histories/views, frozen
  parameters, EMA windows/adjust=False, breakout/ribbon, Wilder ATR, Decimal
  calculations and snapshot construction.
- Strategy: typed opening windows, frame construction, 99 completed candles,
  forming-open-only data, EMA crossover/insufficient history, signal and
  execution decision construction.
- Schedule: view validation, chronology, aligned signal boundaries, completed
  timing and the 4h-signal/15m-execution bridge.
- Data: authorization's structural history checks, exact Decimal sum, normalized
  candles, complete ordered aggregation, view integrity and opening construction.
  Existing local loader structure is also conservatively retained.

## Narrow calendar slots

Only historical AST positions containing the explicitly listed calendar names
(`DEV_START`, `VALIDATION_START`, `HOLDOUT_START`, `START`, `DECEMBER_START`),
calendar imports, `Role` split members or six explicit authorization labels
are normalized in the semantic projection. Their actual AST values form the
separate calendar infrastructure record. Slot positions are derived from the
verified snapshot; current nodes must retain the expected type and allowed form.

This removes calendar-year/split values from candidate identity without removing
history sizes, half-open comparison structure, chronology arithmetic, operators,
boundary alignment or guard invocation order. No expression/function is broadly
excluded, and no regex source extraction is used. A new authorization helper or
structural guard rewrite needs an explicit reviewed scope extension; this version
does not silently accept arbitrary replacement logic as "calendar code".

## Binding and compatibility

The new migration fingerprint binds historical source/software provenance,
selection/trial/result and frozen specification, mixed-module semantic identity,
the remaining whole-file protection/execution scope, and infrastructure identity.
Infrastructure binds the existing explicit infrastructure record, this migration
module's source, schema version, and the actual extracted calendar components.
There is no directory hashing, artifact path, timestamp or randomness in it.

The four modules are not dropped from verification. Numerical or dependency
changes invalidate the semantic comparison; calendar-slot changes alter
infrastructure identity while historical and candidate identities remain stable.
Historical whole-file provenance is still verified separately. Provenance fields
in future runtime output may identify a new source file revision even when its
numerical component identity is unchanged; no historical output is rewritten.

The old `candidate_binding` and all execution gates remain unchanged. Future
Phase 6F0 must explicitly use the new migration binding alongside separately
authorized candidate/split/data/attempt checks. This migration grants no access
or execution permission and does not implement validation support.

Focused tests mutate source strings in memory, not runtime files or artifacts.
The full offline suite includes its existing synthetic fixtures; no research
simulation is launched. Validation has not begun, no 2025/2026 market data is
read, and TRUE_HOLDOUT remains unauthorized.
