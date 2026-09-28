# LIVE-0B B5: dormant operational layer

All outputs are **FORWARD_PAPER / NON_EXECUTING**. No activation boundary,
reviewed real symbol rules, run, task or financial evidence is created by this
implementation. No Binance access is implemented. LIVE-0A remains an independent
collector; B5 can only read its approved local evidence adapters.

## Authorization and layout

`paper.operations.propose` builds immutable canonical bytes from an explicitly
supplied run ID, future UTC 4h T0, source identity and `RuleSnapshot`. The snapshot
reuses B3's exact three-symbol `SymbolRules` model and Decimal validation, with
an explicit version/source digest. No defaults, exchange lookup or rule refresh
are supplied. `rules_from` rejects missing, noncanonical or inconsistent values.

The authorization binds B1 through B5 implementation/runtime identities, frozen
configuration, B2 policy, source run `live-0a-001`, source root/patch identity,
symbol rules and scheduler/recovery policy. Initial equity is 1000 USDT and its
availability is T0. B2's strict **S > T0** gate is retained: a signal at T0 is
history-only, as are all earlier observations. T0 has not been selected.

After separate approval only, `python -m paper activate --run-id <reviewed-id>
--reviewed-manifest <canonical-json>` verifies source authorization and writes an
exclusive, fsynced `activation.json`, containing the authorization and its
recorded SHA-256 fingerprint. Loading compares the recorded fingerprint before
interpreting the authorization, including before the first ledger exists.
It checks future T0 both before and after source verification; the activation
API defaults to a fresh final UTC sample, while tests explicitly inject clocks.
Existing namespaces cannot be replaced; partial activation
is not repaired automatically. Validation/proposal functions do not write files.

Control metadata lives at `artifacts/forward-paper-consumer/<run-id>`. The bound
financial ledger lives at `artifacts/forward-paper-execution/b4-<run-id>`.
B4's foundation namespace and internal identity format remain unchanged; the
outer B5 authorization supplies the real explicit prospective gate and its
authorization fingerprint. This is composition of the existing typed B2/B4
interfaces, not fabricated research objects or a bypass of source checks.
No B1–B4 source or historical implementation identity changes are necessary.

## Consumer

The future command is `python -m paper consume --run-id <reviewed-id>`.
Missing activation refuses before creating a directory, lock or ledger. A
separate OS ownership lock prevents overlapping consumers; B4 also retains its
own ledger lock. Each invocation validates authorization, verifies LIVE-0A,
reconciles B4, and processes a finite snapshot in chronological order.

The consumer uses actual invocation time for acceptance; it never substitutes a
historical timestamp. Late ONLINE signals remain missed, delayed signals remain
delayed, and B2 selects only one symbol without future fallback. Pending/open
exposure is advanced only through exact contiguous received execution candles.
Before processing a signal boundary S, financial replay stops before candle S;
that candle cannot free the slot ahead of arbitration. Missing data retains the
slot. Missing candles below the verified source high-water mark are a blocking
gap, never interpolated. Source observation gaps likewise block processing.

The invocation stops at 128 transitions or a 45-second budget checked between
atomic operations. This is a cooperative bound, not an interruption inside a
SQLite transaction or expensive source verification. Task Scheduler supplies a
two-minute hard limit; a killed process relies on B4 atomicity/replay. Repeated
invocations resume existing instructions; they never infer interrupted B2 timing
confirmation or reconsider blocked candidates. No end-of-data close is invented.

Operational SQLite records attempts, completions, transition counts and source
progress separately from financial accounting. Empty evidence exits WAITING.
Authorization, source or ledger contradictions create a permanent exclusive B5
BLOCKED latch. There is no automatic repair/clear action. A crash can leave an
unfinished RUNNING attempt; reports label it incomplete-or-running rather than
pretending success. B5 failure cannot write LIVE-0A or change its task.

An existing or previously attempted ledger that is missing cannot be recreated
silently. An existing database must already contain its unique identity before
B4 is opened; truncation cannot turn into fresh initialization. Such failures
latch BLOCKED. Reports display unknown equity for incomplete/missing ledgers
rather than resetting the displayed balance to 1000. Durable BLOCKED warnings
take precedence over unresolved-acceptance health labels.

## Read-only reporting

`python -m paper status|latest|trades|missed|summary --run-id <reviewed-id>`
uses SQLite `mode=ro`, `query_only` and a read transaction. It never constructs a
writable `PaperLedger`, acquires its ownership lock, reads market data, calculates
signals or creates repairs. It reuses B4's verification routine against a
read-only connection to compare journal replay and exact materialized rows.
Invalid state raises instead of being normalized or silently trusted.

- status: authorization, health, attempt/success metadata, source progress and
  staleness, global slot, cursors, financial/state availability, BLOCKED warnings.
- latest: source provenance, latest audited decision, reservation/entry/exit state.
- trades: direction/symbol, fills, fees, gross/net P&L and equity before/after.
- missed: B2 audited dispositions and B3 sizing/execution rejection state.
- summary: closed-trade realized equity, wins/losses, gross/net, fees, realized
  drawdown, disposition counts, ONLINE actionable and accepted counts.

Current-position MTM is price-only at an already persisted processed candle
close, with entry fees deferred. It is timestamped and can be stale. A complete
portfolio MTM series is not persisted by B4, so MTM drawdown is explicitly null
with a limitation, never substituted with realized drawdown or intrabar extremes.
No performance interpretation is generated. Operational metadata changes with
each invocation; financial identities remain deterministic.

## Scheduler tooling (not registered)

`scripts/live0b-task.ps1` defaults to Generate and requires RunId, a future UTC
whole-minute StartBoundary and a new OutputPath. It emits BOM-marked UTF-16 XML
matching the UTF-16 declaration and Task Scheduler BSTR representation.

The separate task uses project `.venv/Scripts/pythonw.exe`, the project working
directory, `-m paper consume --run-id <reviewed-id>`, PT1M, IgnoreNew, PT2M,
StartWhenAvailable, InteractiveToken, LeastPrivilege, no idle/network condition,
no automatic restart and **Enabled=false**. Codex-cache runtimes are rejected.
RegisterDisabled is a later explicit action that requires matching reviewed XML,
the stable runtime, valid activation, and absence of an existing task. The script
has no enable/run action. No task is generated in the real artifact namespace or
registered during B5 implementation; tests generate temporary XML only.

## Validation and remaining activation inputs

Focused tests use synthetic temporary source evidence and authorization clocks.
They cover pre/exact/post T0, immutable identities, expired authorization,
arbitration reuse, repeated invocation, locks, chronology/gaps, ledger corruption,
read-only reports and accounting, persisted-only MTM, and actual XML encoding.
Run focused and regression suites with the stable project Python; no credentials
or live data are needed. No dependencies are added.

Future activation still requires reviewed real rules, an explicit future T0,
reviewed authorization fingerprint, and separately approved disabled task
registration/enabling. None of those decisions is supplied by B5.

The two earlier B1/B2 tests asserting that the entire package has no CLI were
scoped to their frozen foundation surface: B5 now owns an explicit CLI. Their
existing import allowlists remain intact. No B1–B4 implementation file changed.

Verified B5 policy fingerprint:
`8e095dd67cfee7f891b482eae4b486991d80b7e28f1ee4781dc5503cce9ca6e0`.

Verified B5 implementation/runtime fingerprint:
`3ad1ab1b37db94d143ff46de6ba7a612d340a4bf61fd6ab35a9fd0674c34bead`.

Inherited B4 remains
`93540210f583d98076f3604408b2aabe4c30cf452cf6cc0f861deabc6f947542`;
integrated B2/B3 and LIVE-0A fingerprints also remain unchanged. These are
implementation identities, not an activation authorization. No run fingerprint
exists until the missing activation inputs are separately reviewed and frozen.
