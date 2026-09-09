# Phase 6B4 — explicit research execution and reporting

This phase implements infrastructure only. No Phase 6C experiment has been
executed, no saved 2024 EMA evaluation has occurred, and no 2025 evaluation or
2026 data access is authorized. Baseline-001 and Phase 6A remain immutable.

## Explicit execution contract

`research.runner.execute` is a keyword-only Python API, not a command-line
entry point. Import does not read market files, register experiments or run
anything. It has no downloader, Binance client, parameter search, implicit
registration, automatic promotion, or validation/holdout execution path.

A future operator must separately authorize execution, register a hypothesis
and immutable `ExperimentSpec`, record DEVELOPMENT access, and explicitly
create an attempt through the Phase 6B1 `Registry`. The caller supplies the
attempt ID, typed split, validated execution datasets, signal views, explicit
`EMACrossover(EMAConfig(9,21))`, full `SimulationConfig`, frozen rules snapshot,
and an output root named `research`.

`planned_batch` represents exactly the 15m/1h/4h concepts without registering
or executing them. It requires caller-supplied view fingerprints and source/
software identity. All three inherit the unchanged Phase 6B1 policy: 1000 USDT,
1% risk, 3x leverage, 1.5% SL, 3% TP, LONG/SHORT, one global position,
BTC→ETH→ADA priority, 15m execution, fee 0.0005 and adverse slippage 0.0002 per
fill, and research-rules-v1. `approved_configuration` is a planning convenience;
execution still requires an explicit matching configuration.

The spec's `development_dataset_fingerprints` bind the three signal-view
fingerprints. Each view binds its exact source 15m dataset. The runner rebuilds
each view from the supplied execution source and verifies equality, preventing
unrelated signal/execution candles from being paired. Source hashes cover all
research/backtest Python files, frozen strategy.py and requirements.txt, with
normalized line endings. Python/pandas/numpy versions must match exactly.
Changing code after registration requires explicit research-budget review;
there is no automatic alternate identity or slot replacement.

Authorization is checked before inspecting candle contents. Only the full
2024 DEVELOPMENT interval is accepted, with 99 completed signal candles of
history-only warmup. No decision, trade or score originates in warmup. Caller
objects are already in memory: this API cannot undo unauthorized access that
occurred outside it. Future file-loading entry points must retain access-before-
read ordering; this phase adds no general file loader.

## Execution and ledger

The runner passes `ResearchSchedule` explicitly to the existing keyword-only
Phase 5E seam. Phase 5D/5E still own sizing, fills, protections, fees, priority,
global position ownership and end-of-data accounting. Signal decisions occur
only at signal openings; position management continues at every 15m boundary.
No baseline source file or default execution behavior is changed.

Registry history retains hypotheses/ancestry, plans, duplicate registrations,
attempts, failures, completed outcomes, disclosures and operator promotions.
`ledger_summary` separates planned configurations, attempts, retries, failures
and completions while retaining the full hash-chained history and rationales.
Retries share the same computational run ID and do not increase independent
trial count. Failed input validation on a known open attempt records FAILED;
unregistered requests cannot create experiments implicitly. Completion is
recorded only after artifacts are written. Viewing/disclosing a result remains
an explicit `record_disclosure` operator action, never inferred automatically.

## Scorecard semantics

`research.scorecard` reuses Phase 5F `analyze`, equity curves, drawdown and trade
metrics. Gross PF is positive gross trade P&L divided by the magnitude of
negative gross trade P&L. Net PF applies the same formula to net trade outcomes.
Zero denominators are JSON null plus an explicit status, never Infinity/NaN.
Positive outcomes with no losses exceed the eligibility PF threshold; no
outcomes do not. Gross/net expectancy is the corresponding arithmetic mean
per completed trade; no trades yields null. Wins/losses use net outcomes.

Quarter/month attribution uses UTC exit-candle year/month, which cannot straddle
a calendar boundary on the aligned 15m grid. All four quarters and all twelve
months appear, including zero-activity periods. Symbol, direction and
symbol+direction groups are alternative partitions, not additive independent
portfolios. All partitions reconcile to completed-trade net P&L.

MTM drawdown is primary and uses the existing candle-close curve; realized
drawdown stays separately labeled. Open-position MTM is price-only, excludes
hypothetical exit fees and defers entry fees until closure. Funding and
liquidation are excluded. Boundary occupancy counts an existing or newly
created position, not exact elapsed intrabar holding time. Trades/day uses the
366-day declared DEVELOPMENT interval, even if insolvency stops execution.

Recorded slippage is the sum of absolute reference/fill price differences times
quantity for actual recorded fills. Fees plus this slippage form the friction
proxy; this is not a counterfactual no-slippage strategy rerun. Filled notional
and filled-notional/starting-equity are turnover proxies. Gross P&L already
reflects slipped fills: the slippage proxy is NOT deducted again from net P&L.

The scorecard fails closed on lifecycle or financial reconciliation failure.
Eligibility requires positive net expectancy, net PF >1, all four quarters
active, at least three positive net quarters, MTM DD strictly <25%, and no
insolvency/integrity/reconciliation failure. Otherwise INELIGIBLE carries
machine-readable reasons. When gates pass, fewer than 100 trades yields
WEAK_EVIDENCE; 100 or more yields ELIGIBLE. The evidence target is not a hard
rejection and does not imply statistical confidence. Failed integrity runs
have ledger failures rather than fabricated completed scorecards.

## Immutable artifacts and comparison

Future output layout:

```
artifacts/research/<experiment-id>/development/
  approved-inputs.json
  first-result.json
  scorecard.json
  report.txt
  run-manifest.json
  fingerprint.txt
```

All output files and the run directory are exclusive creations. Full canonical
financial/decision/boundary records are flushed to `first-result.json` before
analytics. No prior directory, even an incomplete one, is silently reused.
The run manifest records SHA-256 checksums of the input, result, scorecard and
human report files in addition to their computational fingerprints.
After interruption, preserve the directory and checkpoint for manual recovery;
do not retry simulation to repair reporting. Automated checkpoint reload/
report-only recovery is deferred. A ledger failure after complete file writing
also requires reconciliation, never a rerun or silent outcome replacement.

Canonical JSON uses Decimal strings and SHA-256; no wall-clock timestamps,
random IDs or output paths enter computational fingerprints. Registry attempt
sequence is ledger provenance; retries retain computational run identity.
Research artifacts and ledgers under `artifacts/research/` are ignored by Git.

`compare_batch` accepts exactly the three predeclared specs and their completed
scorecards. It displays performance, costs, risk, temporal stability,
attribution, operations and evidence side by side. Pareto dominance is explicitly
limited to higher return, higher net expectancy and lower MTM drawdown; undefined
expectancy is incomparable. Other dimensions remain visible, with no scalar
score, total ranking, winner nomination or automatic validation promotion.
`write_comparison` exclusively creates JSON and human-readable comparison files.

All results share the same observed DEVELOPMENT period and are correlated;
stronger performance is not independent confirmation. 2025 was already observed
through baseline-001 and Phase 6A and remains locked, previously observed
validation. 2026 remains untouched TRUE_HOLDOUT with endpoint unset until a
separate future authorization. No report claims future profitability.

## Verification scope

Offline tests cover authorization before data inspection, exact identity and
cost matching, split denial, retries, immutable writes, reporting failures,
metric edges, attribution, eligibility, Pareto behavior and determinism.
A test-only bounded clock drives three synthetic openings, including a
crossover entry and stop exit, through the actual research scheduler and
financial primitives; no saved market data is read.
Existing baseline regression tests remain unchanged.
