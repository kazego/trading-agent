# Phase 6F0 structural validation preflight

The first stop was the legacy entire-tree source coupling. Phase 6F-Identity
added historical verification independent of today's code. The second stop
was mixed-module calendar/numerical coupling; Identity2 added a conservative
semantic projection and migration binding. Their historical fingerprints remain
unchanged. This phase adds only `research.validation` and metadata-only tests.

The explicit candidate is `phase6d-c-atr-v1`, with its exact frozen specification,
Phase 6E selection, historical C result, candidate semantics and original
migration fingerprint. New infrastructure identity binds that migration's
infrastructure, the explicitly scoped validation module, schema and software.
The complete binding belongs in future validation artifacts; no old record is
rewritten and no hidden parameter default is substituted.

Evaluable interval: **[2025-01-01T00:00Z, 2026-01-01T00:00Z)**.
Warmup: **[2024-09-23T00:00Z, 2025-01-01T00:00Z)**, exactly 600 completed
4h candles, HISTORY_ONLY and non-evaluable. Rolling history ends at the decision
opening, excluding its forming candle. Execution boundaries align to 15m and
signal boundaries to 4h. No 2026 boundary or history dependency is accepted.

Reporting classification is exactly **LOCKED_PREVIOUSLY_OBSERVED_VALIDATION**.
Development eligibility is not applied as validation criteria. Validation
criteria remain unset; no performance classification is produced here.

`plan_validation(root, frozen_spec=..., software=...)` requires explicit input.
`preflight(root, plan, artifact_root=...)` reconstructs and exactly compares all
metadata and checks pristine accounting and collision absence. It returns a dry
`ValidationContext` and READY with scope STRUCTURAL_ONLY. This is not permission
to read market data or execute. No data or numerical runner is invoked.

Output namespace is `artifacts/research/<plan fingerprint>/validation`, never
`development`. Complete or partial existing paths fail closed. Future validation
attempts have a separate `artifacts/research/validation-registry/events.jsonl`
namespace, preserving the immutable DEVELOPMENT ledger checkpoint. This phase
only inspects the namespace; zero events/attempts are required. No registry or
artifact directory is created by planning or preflight.

Existing development data/feature/scheduler gates remain unchanged. The dry
context is structural infrastructure, not an executable adapter: later authorized
Phase 6F work must integrate actual validated data and numerical runner inputs,
check the new binding at execution, and explicitly authorize/record the attempt.
READY here does not claim live or end-to-end execution compatibility.

2024 development remains closed to further tuning. A/B/reference candidates,
changed configurations, intervals, labels and fingerprints reject. TRUE_HOLDOUT
remains unauthorized. Validation has not begun and no 2025/2026 market data was
accessed. No simulation/backtest or financial result was generated.
