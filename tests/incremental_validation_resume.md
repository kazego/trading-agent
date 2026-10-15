# Recovered memory matrix and financial scenario validation

## Preserved completed run

The previous session completed with exit code 0. Its JSONL ends in
`matrix_complete`, has exactly 30 sequential sample IDs for every size, and all
summary median/p95/max values were recomputed from the individual samples. All
four final full audits report PASS. No matrix was rerun in this continuation.
The in-memory fixtures were closed by the original driver and are not retained.

Unchanged source SHA-256s:

- `research/incremental_ledger.py`: `b6ecf22b2cb56a279701a2922ce2456bb248248b6fab6294c5ee177cc2d5df9f`
- `tests/benchmark_incremental_memory.py`: `25be507c29009f061087beec4b4e78fa078554b5b55e83aa90529f5be61ae04e`
- Preserved `benchmark_incremental_memory.results.jsonl`: `d125974239d080a16e828b2c4cc19ca1db6398464126e9b216a13b2bf9871779`

| Initial events | n | Median s | p95 s | Max s | Compact bytes | Audit |
|---:|---:|---:|---:|---:|---:|---|
|348|30|0.261209|0.306425|0.330303|9203|PASS|
|700|30|0.256941|0.416230|0.757668|9203|PASS|
|7000|30|0.196233|0.198742|0.200438|9204|PASS|
|70000|30|0.388298|0.489813|0.576915|9205|PASS|

Time includes core-object initialization, local verification, one non-exiting BAR,
in-memory SQLite commits and memory continuity publication. Python startup, real
authorization/source loaders, disk and durability are absent. Wrapping/timing
overhead is included. Thirty consecutive transitions take N to N+30.

Preparation and complete replays are outside each timed sample:

| Events | Fixture preparation s | Full bootstrap s | Final full audit s |
|---:|---:|---:|---:|
|348|11.132|11.130|12.195|
|700|24.719|24.453|26.424|
|7000|247.423|221.145|370.882|
|70000|4197.717|4195.993|3812.356|

700 -> 70000 total p95 growth: 17.678%; reducer inclusive p95 growth: 6.986%.
Median total grows 51.1%; 7000 -> 70000 p95 is about 2.46x. These sequential
batches do not isolate CPU/load/thermal/time effects. The evidence does not show
linear 100x growth, but is not proof of history-independent latency or production
readiness. Compact size grows two bytes (numeric sequence width), not retained
historical rows. Do not add overlapping inclusive stage times.

## New financial scenarios: failed, do not benchmark around the failure

Added `benchmark_incremental_financial.py` and `test_incremental_financial.py`.
The intended workload alternates directions, TP/SL, opening, v2 fixture timing
confirmation, fees, continued open bars, exact closed-trade retries and multiple
trades. The second test includes gap stops and dual-touch cases. This is not a
clock-v3 integration test and grants no production authority.

Both tests fail at the first BAR immediately following a new ENTRY:

`research.incremental_ledger.Blocked: Conflicting existing financial row`

Diagnosis: `compact` retains the entry evidence in `position.entry`, but `restore`
only places the maximum processed witness into `s.candles`. Immediately after ENTRY
there is no processed witness. Therefore `before.rows()['candles']` omits the entry
candle already present in SQLite. Applying the first BAR adds that same candle to
`after`; `_write_delta` finds an existing immutable row absent from `before` and
fails closed, although its bytes exactly equal the expected evidence.

The earlier matrix starts after historical BAR processing and has a witness, so
it does not cover this boundary. It remains a valid measurement of its stated
workload, but cannot certify the missing ENTRY -> first BAR path.

Diagnostic reproduction (synthetic only):

- Conflicting table: `candles`; existing row equals expected row exactly.
- Rollback leaves all inspected tables logically identical, including journal,
  financial projections and compact state; external continuity unchanged.
- Journal stays at 10 events; three fills (seed entry/exit + new entry), one trade.
- Independent full audit after failure: PASS.

No core fix was applied. Proposed minimal correction for review: restore the
verified entry candle into the active pre-state's candle mapping, in addition to
the maximum-receipt witness. Retain the strict conflicting-row check; do not use
INSERT OR IGNORE or accept arbitrary existing immutable rows. Then prove compact
roundtrip, source identity, first-bar equality, multi-trade accounting, retries
and full final reconciliation before latency measurements.

Run the failing tests reproducibly (memory only):

```powershell
.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_incremental_financial.py -v
```

No 60-cycle financial benchmark was executed after the failure; no SL/TP/opening
latency distribution is claimed. Do not run the proposed benchmark as though the
tests pass.

## Large fixture reuse proposal (not executed)

The recovered 70000 fixture already consumed roughly 203.4 minutes in preparation
and full audits. Do not repeat it for this report. For future approved validation,
build once and retain the synthetic database/verified anchor in one process for
all scenario batches. Separate preparation, initial full verification, each batch,
and full final audit. This avoids rebuilding for each scenario, not any financial
check. Reusing results requires unchanged code/config and workload provenance.

If persistence is later authorized in a functioning isolated environment, archive
a synthetic fixture with source/config hashes, complete audit record and exact
SQLite bytes. On reload still perform full bootstrap verification; a file hash
alone cannot grant trust. This saves construction time, not required verification.
No persistence path, subprocess worker or cross-invocation production cache is
introduced here.

The existing `incremental_filesystem_plan.md` remains an unexecuted plan. No live
database, task, authorization, position, consumer or network was accessed/changed.

## Authorized entry-candle correction — 2026-10-14

The prior blocked result above is historical. `restore()` now retains
`evidence.record` under `evidence.id` immediately after reconstructing the active
position. No schema, compact serialization, financial formula or conflict guard
changed. The original entry evidence is already retained in compact state.

Validation: 30 incremental tests passed; the three financial tests were rerun
after covering TP/SL in both directions and passed. Identical candle rows are
recognized; different immutable content at the same key still raises
`Conflicting existing financial row`. Existing rollback, corruption, audit expiry,
restart and idempotency tests passed.

Memory-only benchmark: 120 cycles, alternating LONG/SHORT, modes TP/TP/SL/SL/
DUAL/DUAL/GAP_SL/GAP_SL. Each close mode has 30 samples. No large matrix rerun.

| Stage | n | Median s | p95 s | Max s |
|---|---:|---:|---:|---:|
| ACCEPT | 120 | .059650 | .076157 | .112560 |
| CONFIRM v2 fixture | 120 | .058064 | .080632 | .103156 |
| ENTRY | 120 | .125512 | .153524 | .207221 |
| First BAR | 120 | .230217 | .270274 | .373810 |
| TP close | 30 | .273432 | .388490 | .409427 |
| SL close | 30 | .259958 | .360058 | .379651 |
| Dual-touch close | 30 | .263814 | .311825 | .339013 |
| Gap SL close | 30 | .264269 | .355046 | .410890 |
| Closed retry | 120 | .041815 | .052023 | .079129 |

Includes object initialization, local validation, transition, in-memory SQLite
commit and in-memory continuity publication. Differential reference checks and
the full final audit (22.330609s, PASS) are outside samples. This does not validate
disk durability, fresh processes, real authorization/source loaders or LIVE-0B.

Final exact reference/projection equality: 607 journal events, 121 closed trades
(including seed), 242 fills, equity 668.445306348558, total fees 25.843061047442,
net P&L -331.554693651442, EMPTY slot. Forced stops make this a correctness workload,
not a strategy result. Active first-BAR envelope: 9231..9248 bytes (first9231,
last9246); closed envelope6109..6119. No history arrays grow with trade count;
scalar number/Decimal string lengths can change. Old benchmark archive unchanged.

### Long audit design only; not implemented

Current audit holds ExternalMemory's in-process exclusive lock plus a SQLite
read transaction throughout replay. At 70000 events the recovered full audit
took3812.356s (63.54min); this is not a measured snapshot-copy cost.

Alternative1: retain exclusive writer ownership for complete audit. Simplest
consistent exact-head result, but pauses transitions for the entire audit.

Recommended future alternative2: under real cross-process exclusive ownership,
finish/recover COMMIT/publication, capture an exact SQLite snapshot and matching
independent continuity anchor, then release ownership. Audit a private sealed
snapshot with no writers. A readonly connection alone does not prevent another
process writing: filesystem ownership/access controls and the snapshot writer
protocol must establish that guarantee, without bypassing existing sandbox rules.
Use supported SQLite backup/snapshot semantics, never copy a live main DB alone
while ignoring WAL. Copy latency, disk size, locks and durability remain unmeasured.

The verdict binds captured sequence/head, genesis, schema/reducer/authorization,
snapshot identity and capture time. Publish under ownership only after checking
unchanged ancestry, no rollback and that current history extends the certified
prefix. It certifies no newer suffix. Age must not be rejuvenated by a long audit:
use capture time, and count events since certified sequence. Keep current
24h/1000-event policy unchanged; reaching either limit, missing verdict, failure
or divergence blocks transitions even while an audit runs. A completed but already
expired verdict cannot unblock. Snapshot capture, protected verdict persistence,
cross-process exclusion, crash recovery and prefix continuity require dedicated
filesystem tests before adoption. No implementation in this correction.
