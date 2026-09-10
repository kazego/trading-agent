# Phase 6D5: repaired integration and dry preflight

The blocking metadata-only preflight has been replaced. The final local check
returned **READY**: all three executable contexts were constructed from verified
2023 HISTORY_ONLY and 2024 DEVELOPMENT inputs. No feature boundary was evaluated,
no signal generated, no simulation run, and no A/B/C performance calculated.
No Phase 6D attempt or completed financial artifact was created.

## Frozen plan and concrete mapping

| Trial ID | Concrete strategy | Explicit protection |
| --- | --- | --- |
| `phase6d-a-ribbon-v1` | `RibbonStrategy`: EMA_CROSSOVER_RIBBON / ema9-21-ribbon20-50-100-200-4h-v1 | Existing fixed 1.5% stop / 3% target |
| `phase6d-b-breakout-v1` | `BreakoutStrategy`: FRESH_BREAKOUT_TREND / fresh-breakout20-ema200-4h-v1 | Existing fixed 1.5% stop / 3% target |
| `phase6d-c-atr-v1` | Unchanged `EMACrossover(EMAConfig(9,21))` / research-ema-opening-v1 | `ATRProtection`: Wilder ATR14, 2 ATR stop / 4 ATR target |

The registration table resolves the complete strategy family/version and
protection family/version tuple to concrete types. Unknown tuples fail closed.
Neither trial names nor parameter shapes select an implementation. The immutable
plan includes the full exact parameters, including the feature initialization
policy, risk basis, configuration, data, source and software identities.

All trials use 4h signals, 15m execution, BTC → ETH → ADA priority, one position,
1,000 USDT initial equity, 1% planned risk and a 3x leverage cap. The existing
modeled fees (0.0005 per fill) and adverse slippage (2 bps per fill) are unchanged
research assumptions. Existing sizing, fill, gap, dual-touch, EOD and financial
logic is reused. Funding, margin and liquidation realism remain separate work.

`TrialSchedule` validates the opening event and authorized signal view before
calling the frozen A/B implementation through a completed `FeatureSnapshot`.
The typed variant decision envelope retains parameters, trigger and feature
identity. C uses the existing EMA research schedule; its protection provider
passes the exact saved symbol/entry-boundary snapshot to the unchanged
`ATRProtection`. Missing or stale bindings fail; C cannot fall back to fixed
protection. A/B explicitly select the existing fixed path and reject ATR requests.

## Canonical API and preflight guarantee

```python
from research.phase6d5 import trial_plan, preflight

plan = trial_plan()
result = preflight(plan)  # Uses the already explicitly registered local plan.
print(result.record)
contexts = result.contexts
```

`TrialPlan` owns immutable canonical JSON bytes. `record` returns a detached
copy; `TrialPlan(plan.payload)` round-trips directly. No dictionary/dataclass
conversion is needed by preflight. Floats, nonfinite numbers, duplicate JSON keys
and noncanonical serialized input are rejected.

Preflight compares the entire plan with the approved template and the persisted
registration, then verifies the exact DEVELOPMENT split, full policy, eligibility,
source/software, feature implementation/version, configuration and rules hashes.
It validates the pinned acquisition and warmup manifests semantically by their
canonical fingerprints. Empty, missing, malformed or altered fields fail.
It uses the existing checksum-verifying loader for December 2023 and 2024 only,
rebuilds the 4h view, verifies dataset/view fingerprints, and constructs the
verified 600-candle HISTORY_ONLY `FeatureView` for every symbol. It does not infer
validity from a filename. Rules are parsed with the existing typed rule model.

READY therefore includes authorized input reads and aggregation/validation,
but **no candle evaluation**: no `at_open`, feature calculation, strategy call,
simulation, scorecard, execution attempt or result write. Tests and the local
preflight install fail-fast sentinels on those prohibited evaluation operations.
The API has no execution CLI or import side effects.

The future `TrialContext.bind_runner` requires a matching explicit nonterminal
registered attempt, rechecks inputs and policy, and returns a callable without
invoking it. It supplies the existing shared artifact/financial lifecycle with
the exact schedule and protection provider. The scorecard adapter preserves
existing eligibility calculations while disclosing the actual variant policy;
it is not invoked by preflight.

## Ledger, accounting and output safety

`Phase6DRegistry` extends the existing exclusive-writer, append-only hash chain.
One `PHASE6D_PLAN` event atomically freezes all three specifications. Repeating
the identical registration is idempotent; replacing it is rejected. Access and
attempt records retain existing retry/terminal-outcome rules. Failed attempts
can retry only the same identity, never a new independent strategy configuration.

Accounting verifies the three preserved Phase 6C experiment IDs and specifications
(15m, 1h, 4h), their unchanged policy and one completed DEVELOPMENT outcome each.
Adding/removing a historical configuration fails reconciliation. The three new
plans bring the eventual configuration count to six; Phase 6D4 diagnostics count
as zero. Current Phase 6D attempts: **0**. No historical trial is reset or hidden.

The local ledger now has 17 events. Its original 16-event byte prefix is unchanged:
`b3ba2466df887aa143fd9b0f4bedef996591767af3c7b611cd1cffc8eab690d7`.
New registration event:
`f1cd1470ce18702726ac60f5d30b1a700d15702924a78c86be966c576eff0a5b`.
The ignored ledger is metadata only; no new financial output exists.

Preflight and execution share `research.runner.artifact_destination`:
`artifacts/research/<computational trial SHA-256>/development`.
Any complete or partial destination blocks a run. Misplaced readable-name output
directories and redirected/invalid parents fail preflight. Writes remain exclusive;
there is no overwrite or automatic recovery. Interrupted-output handling remains
a separate explicit decision, never a reason to regenerate a completed result.

## Verified identities

Final repaired plan:
`4172bd2610e0c51d8ef4df73aad21f3afe06656ca5f71e1ae5441ad650ca785b`

- A: `d38d22168534f464ff0417073968da89757b8e3a7dd5690ffda17dc69077ebe9`
- B: `06ca49de43b275a088a89a137dd1c947c59ffdbc77d9e8256302b16970b265b2`
- C: `689c90ddee96b7239b12e53714e05c024e7b63bc30d6745d0d54b11ef561b1be`

Old incorrect reported plan:
`079a9eb0b6e3610ac9c06768a17bb6a5e2327ddfe3559a96eeca6b7849a4ba17`.
Actual pre-repair plan:
`e49cadfa9049b6eba6a74373718660d0b19c89a863c9d2d51a6c13fe5cd7791f`.
The repaired value intentionally differs: schema, identity coverage and executable
source changed. Source hashes participate in identity; later source edits invalidate
the registered plan and cannot silently create a modified retry. Paths, wall-clock
time, retrieval time and documentation edits do not participate.

Complete frozen Phase 6 policy:
`2642052de313ece07b300791ab1663ff60fc8368d9d7d0059934cde75d4dac99`.
Unchanged eligibility subsection:
`e8f4c0d736c8a8c29e3dc26e61aa3bf194e519eeb9db16e614190f19054cb348`.
These hash different scopes, not different eligibility semantics.

Eligibility remains positive net expectancy, net PF > 1, four active quarters,
at least three positive quarters, MTM drawdown strictly < 25%, and no integrity,
insolvency or unexplained reconciliation failure. The 100-trade evidence target
is not a hard rejection. No new score, ranking or automatic promotion exists.

2025 remains LOCKED_PREVIOUSLY_OBSERVED_VALIDATION, not unseen data; 2026 remains
untouched TRUE_HOLDOUT. Both are denied by the Phase 6D access path. Validation
requires a separate future decision after all results, with at most one candidate;
this phase grants none. Further 2024 investigation increases overfitting risk.

## Verification and limits

25 focused repair tests cover canonical round-trip, each important fingerprint
substitution, malformed manifests, exact mappings, dry contexts, snapshot handoff,
ledger accounting/corruption/retries, access denial, shared destinations and
collisions. The complete offline suite ran once after repair: **464 passed**.
No dependencies changed. Frozen strategy, features, ATR mathematics, diagnostics
and eligibility code remain unchanged; default baseline tests still pass.

READY is integration evidence, not executed A/B/C evidence or a live-market
compatibility claim. No Phase 6D strategy experiment, A/B/C performance calculation,
2025 market-data read, 2026 access, commit or Phase 6D6 work occurred.
