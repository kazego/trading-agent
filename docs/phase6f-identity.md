# Phase 6F-Identity: provenance separation

Phase 6F0 stopped because legacy `trial_plan()` embeds every research/backtest
Python file hash. Reconstructing that plan after adding infrastructure changes
its identity. Equality with today's reconstructed plan is an execution gate,
not proof of historical provenance.

`research.identity.verify_history(root)` is the explicit read-only legacy path.
It verifies the frozen selection, six preserved Phase 6C/D manifests, their
exact file checksums, recorded result references, scorecards, comparison, plan,
policy and ledger chain/checkpoint. Financial results and their fingerprints
are not regenerated. No old artifact is migrated or written.

Every recorded source hash is checked against its historical Git snapshot:

- Phase 6C: `c70d7e618e34832e5076c8ffcd6724353514fd39`
- Phase 6D: `313f8bd9de8210fb113850813b4f2551789c686c`

The legacy universal-newline UTF-8 hash convention is retained. Phase 6C source
and software pair lists are explicitly normalized to mappings for comparison.
Ledger completed outcomes reference scorecards, not portfolio-result hashes.
Historical software versions are preserved and cross-checked with approved
inputs; this does not independently attest what runtime actually executed.

The new `scoped-validation-identity-v1` binding contains historical trial/result
and selection references, full recorded historical source/software provenance,
candidate semantics with an explicit source scope, and a separate current
infrastructure record/fingerprint. SHA-256 and existing canonical JSON are used.
No paths to local artifacts, wall-clock values or random identifiers enter it.

The candidate scope is explicitly enumerated by `SEMANTIC_FILES`: strategy.py,
requirements, package initializers, baseline data/clock/strategy adapter,
simulation/portfolio, and research data/features/strategy/schedule/protection.
It freezes EMA9/21, 4h signals, 15m execution, ATR14/2x/4x frozen at entry,
no trailing, universe/priority/one position, risk/leverage, costs, sizing,
fills, gaps and EOD. Parameters and protection behavior are also bound directly.
Any changed parameter, scoped source hash, policy or runtime is rejected.

Infrastructure scope is explicitly `research/identity.py`, `research/spec.py`,
`research/registry.py`, `research/runner.py`, `research/phase6d5.py`,
`research/scorecard.py`, `backtest/metrics.py`, and `backtest/report.py`.
Reporting/orchestration changes alter that fingerprint without rewriting
historical provenance. Unlisted files are not silently considered active
infrastructure: future integration must explicitly enroll its new modules and
review the scope/version. There are no directory globs in the new model.

Mixed semantic/data files are conservatively locked whole-file. A future split
support edit in one of those files still needs a reviewed scope migration; this
phase does not grant a blanket exception for changes described as infrastructure.
Runner defaults are not authority for parameters: the explicit frozen spec is
required. Infrastructure fingerprints identify code; they do not approve it.

`candidate_binding(root, proposed_spec, software)` performs both historical and
current semantic checks and returns detached canonical-compatible metadata.
Future Phase 6F artifacts must store the entire binding and its fingerprint,
plus separately authorized split/data/attempt identities. This is not an access
token or executable context. Existing execution checks remain unchanged.
The historical ledger checkpoint is exact: a future appended ledger requires
an explicit checkpoint/prefix verification extension, never silent acceptance.

Focused tests cover historical snapshots, preserved evidence integrity and
read-only operation, parameter/source tampering, infrastructure independence,
canonical input rejection and deterministic binding. The full offline suite
includes its existing synthetic execution fixtures; no research run is invoked.

No market evaluation, validation attempt or holdout authorization is performed.
No 2025/2026 market data is read. Phase 6F0 and Phase 6F are not resumed.
