# LIVE-0B B1 — isolated foundations

All identities and evidence envelopes are **FORWARD_PAPER / NON_EXECUTING**.
B1 does not activate a run, select a prospective start, accept a signal, reserve
an entry, simulate a fill, or create a position/trade. There is no CLI, consumer,
scheduler, collector, credential handling, Binance SDK or network route.

## Models and identity

`paper.model.PaperConfig` is a frozen Decimal configuration/specification of the
approved candidate C and paper rules. It records EMA9/21 on completed 4h history,
99 completed EMA rows, ATR14 frozen at entry, 2 ATR stop / 4 ATR target, 1% risk,
3x maximum leverage, 1000 USDT initial capital, fees 0.0005 and adverse slippage
0.0002 per fill. These are specification fields, not implemented execution.
BTCUSDT → ETHUSDT → ADAUSDT priority and one global exposure slot are frozen.
Context remains descriptive only. Funding/liquidation remain excluded.

The entry specification is E=S+15 minutes, acceptance deadline E−60 seconds,
one symbol selected at acceptance, with no lower-priority fallback based on
future entry information. Later deterministic settlement requires a previously
committed instruction. B1 implements none of that arbitration or settlement.

`SourceIdentity` explicitly binds the original LIVE-0A manifest, operations root,
operational patch, authorized implementation, Semantic Contract V2 and source
boundary metadata. `DraftManifest` binds that identity, configuration, current
scoped B1 implementation/runtime identity and an optional frozen rules snapshot
fingerprint. It rejects any prospective start timestamp. State is always
`DRAFT_NOT_ACTIVATED`; an absent rules snapshot is explicitly unresolved, never
invented. Activation and its exact future boundary remain for later review.

Canonical JSON and SHA-256 reuse the existing pure utilities. Financial values
are canonical Decimal strings; floats and nonfinite values are rejected.
Frozen objects contain only immutable fields. Returned record dictionaries are
fresh copies. IDs contain no wall-clock timestamp or local path. Implementation
identity separately includes explicitly scoped source files and Python/pandas/
NumPy versions. Merely adding this package does not change LIVE-0A's identity.

## Read-only source boundary

`SourceAdapter(directory, expected).snapshot()` is the sole source operation.
It invokes the unchanged normal LIVE-0A manifest/operations loader, including
the existing historical provenance and root→patch gates. It additionally matches
the explicit expected identities, verifies both evidence checkpoints in its
read transaction and refuses BLOCKED sources.

The existing `forward.readout.connect` uses SQLite `mode=ro`, `query_only=ON`
and a read transaction. No writable LIVE-0A `Store` is constructed. No connection,
SQL execution method, writer or collector is exposed by the adapter. SQLite
read-only mode still rejects writes if query_only is disabled. As with any local
Python library, this API is not an OS sandbox against hostile code running as
the same Windows user; filesystem permissions remain the OS security boundary.

Results are immutable `SourceObservation` values containing canonical original
observation bytes and optional original receipt bytes. Per-symbol decision and
descriptive-context hashes, observation identity, receipt ancestry and existing
timing classification are checked; no signals are recalculated. Missing receipts
remain UNCONFIRMED. Legacy timing is preserved without fabricating receipts.
These are evidence copies, not acceptance approvals. Future acceptance must use
the authorized adapter and enforce its own frozen timeliness/eligibility gates.
In particular, a valid zero ATR remains source evidence; B1 does not decide
whether that observation can support a future entry.

LIVE-0A can append concurrently. A snapshot is transactionally consistent, but
not a freeze of the producer; later snapshots may include new observations or
receipts. Original payloads are never rewritten or relabeled.

## Separate SQLite foundation

`PaperStore` accepts only an explicitly supplied draft and a directory under
`artifacts/forward-paper-execution/b1-*`, creating `paper.sqlite` there. It rejects
LIVE-0A paths, activation-style run names and aliased database paths. This namespace
is ignored by Git. B1 creates no actual runtime namespace during implementation;
tests use temporary directories only.

| Table | Purpose / invariant |
|---|---|
| identity | One canonical draft and fingerprint, checked at reopen |
| source_observations | Original canonical payload and digest; unique source + boundary |
| source_receipts | At most one exact receipt per observation, foreign-key bound |
| evidence | Append-only canonical observation/receipt provenance envelopes |
| global_slot | Exactly one singleton slot, initially EMPTY; valid future states PENDING / OPEN / BLOCKED |

The global slot primary key and CHECK(singleton=1) apply across all three symbols,
not once per symbol. B1 exposes no reservation/transition API and refuses to open
a draft containing exposure. Future phases must implement atomic owner-checked
transitions; the schema alone is not a full execution state machine.

All financial specification values live as Decimal strings in canonical TEXT;
there are no SQLite REAL columns, trade tables or equity accounting. Identity
and provenance tables prohibit updates/deletes. `record_source` is atomic and
idempotent; conflicts fail closed. A later receipt appends its own row and a new
evidence envelope while preserving the receipt-less envelope and raw observation.
Reopening checks integrity, foreign keys, manifest binding and stored provenance.
The existing OS ownership lock is used only for the separate paper database.

## Verification and limits

Focused offline tests cover immutable canonical identities, Decimal rejection,
explicit authorization, genuine root/patch verification against synthetic source
fixtures, checkpoint/observation/receipt tampering, SQLite read-only enforcement,
separate storage, atomic rollback, idempotence, late receipts, global slot
uniqueness, corruption on reopen and an import allowlist excluding collectors,
SDKs and execution modules. Synthetic fixtures substitute the manifest loader
input; a separate test proves its errors propagate without bypass. The existing
LIVE-0A suite remains responsible for the full historical manifest gate.

No production database is opened by these tests. No real observation/evidence is
copied, no market data is fetched and no task is created or changed. No new
dependencies, runtime refactoring or changes to historical artifacts are required.
B2–B5 and activation remain unimplemented.
