# LIVE-0B B6 — invocation-scoped verified source

FORWARD_PAPER / NON_EXECUTING. Implemented only in the isolated
`codex/live0b-b6` worktree based on
`0a0b3f9b6898742030392b62a45720b10c9202c7`. Not deployed or activated.

## Execution boundary

Each post-T0 consumer invocation freshly loads the immutable activation (and,
for the existing run, its explicit B6 transition). Before financial processing,
`paper.verified_source.capture` verifies the frozen LIVE-0A manifest, original
operational root, exact patch ancestry/effective time, current implementation,
semantic reference files and historical source bytes. It retrieves nine legacy
semantic blobs and the reviewed operations blob in **one** `git cat-file --batch`
process. Responses require bounded headers, blob type, exact lengths, valid
content/object hashes, separators, completeness and no trailing response data.
Windows launches use CREATE_NO_WINDOW; other platforms use zero creation flags.
The old forward modules are unchanged and are not invoked as repeated Git
verification paths by the B6 consumer.

One SQLite `mode=ro`, `query_only` transaction runs the existing full Store
verification once. Root and patch checkpoints, receipts, chronology and all
normalized candles are checked in that same transaction. The connection closes
before B4 processing. Frozen observations, CandleEvidence objects and an
immutable candle mapping form the detached VerifiedSourceSnapshot. Progress and
the full captured checkpoint are canonical immutable bytes; its ID binds the
source identity and checkpoint. There is no process-global or cross-invocation
trust cache.

A concurrent append or late receipt is invisible after capture and becomes
eligible for inspection next invocation. Capture time is never acceptance time:
B2 retains actual UTC/monotonic samples and the inclusive S+14-minute deadline.
A boundary missed while verification runs remains late. Missing execution bars
remain unresolved; previously consumed evidence must match the captured view.
Source BLOCKED and immutable authorization/current-source hashes are checked
again before executable transitions and at invocation completion. Captured
bytes cannot change. Subsequent external database corruption is detected at the
next fresh capture; this does not promise instantaneous detection of a database
change after its snapshot has closed.

The monotonic 45-second invocation budget starts before authorization and source
verification. Git has a maximum 15-second timeout bounded by remaining budget.
A Git timeout produces BUDGET_REACHED without trusting partial evidence or
clearing any latch. Integrity failures remain fail-closed. This is a transition
budget, not an asynchronous interruption of an already-running SQLite verifier.
PT1M/PT2M/IgnoreNew and all financial/strategy/timing formulas are unchanged.

## Financial and historical identity separation

B1/B2/B3, journal financial reduction, LIVE-0A code, source authorization,
strategy/context, rules, costs, sizing and both task scripts are unchanged.
B4 receives the detached snapshot through an explicit keyword-only interface;
its financial replay, transactional materialization, global slot and confirmation
logic remain independent. It rehashes the current B4 implementation while keeping
an explicitly authorized historical genesis implementation for old journals.
No historical journal result is rewritten or exempted from financial replay.
Read-only reports perform the same replay without constructing a writable ledger.

The known live run authorization was reconstructed using its previously reviewed
public rules fixture and recorded metadata, without opening live authorization or
databases. The exact identities reproduce:

- activation: `b513a8397ae2210ec4d83cab6f21004586893b7dec358d4181c2c3d383d2a902`
- genesis: `4c312960fc132b0b2c0d8fe874efb16aa29c65d3393d69af2cc9ea2656210769`
- rules: `c968ebf85ebdae012bfe194b87b8b4b55ebd9736683f6ed0de362c928e9afadb`

Synthetic historical NONE journals and closed-trade continuation/restart validate
compatibility. This is fixture validation, not an audit of the changing live
journal. A fresh live checkpoint/reconciliation remains required at cutover.

## Immutable transition

`paper.transition` supports one explicit predecessor (the committed A1 B4/B5
identities). `plan` is read-only; `bind` requires an exact approved proposal hash,
a future effective time, the consumer ownership lock and an exclusive new file.
Neither is called automatically. No real transition artifact has been written.

The proposed filename is `transition-b6-v1.json`, next to the original activation.
Its canonical body binds label/version/scope, original activation fingerprint,
old/new implementation maps, unchanged T0, RuleSnapshot, source authorization,
B2 policy and B5 policy fingerprint, effective timestamp, historical genesis,
canonical identity, journal prefix count/hash, account and global slot checkpoint.
The envelope adds `transition_fingerprint`. Checkpoints permit append-only growth
but reject changed/truncated historical prefixes. The original activation stays
byte-for-byte intact. Missing, altered, wrong-ancestry, not-yet-effective or
unknown-implementation bindings fail closed. There is no general upgrade cache
or implicit rollback mechanism.

## Identities (Python 3.12.10 / pandas 3.0.1 / NumPy 2.3.5)

| Binding | Before | B6 |
|---|---|---|
| B4 | `217d636cc701b0b88ce6f06dac4b27a4ff7380d37827a0efdc33132206a3da32` | `0c3543daf8499baaaf969a76f077f7dd6a34accdbe9cf31b17c3b4fb6e4cde78` |
| B5/runtime | `3e16cfc0fb14a9a2710b80d9ba00e3669875943cb206d0d112e12301280c3a31` | `c27c64c7ff65b70e7493009f902fa1d62e02f4183af4e292e1b817804c48efc5` |

Unchanged identities:

- B1: `f3f5e761bcbda57ff75dfb2d514bfc17cd8a54ff57dc79e7e36e5bd4e8aab353`
- B2: `d481c53caeae3246958ad6baf6aa3e90ef7c813ad47285d18eb09aca642238c1`
- B3: `ae8493a638ee9017df9641a12bbe910fd1d0cde0572ff907df99e1c2cb056d94`
- B2 policy: `dc017143a1e7677e61a868496281afb1254b972a41bf88563bfa11ab3ef10184`
- B5 policy: `8e095dd67cfee7f891b482eae4b486991d80b7e28f1ee4781dc5503cce9ca6e0`
- LIVE-0A: `d7e7a2f461f5060654f13daebb36415d3bd873f1aa9018ae28988f262fdb3b7a`
- Semantic Contract V2: `b9db00e5e2ec6fcf6cc34cde7c2144a3bdba73556c8d2bb05c7c7450cf68c27d`

Prior B4/B5 hashes were independently reproduced from approved fixed Git blobs.
The B6 runtime hash includes all three new runtime modules. No dependency changed.

## Offline evidence

Focused suite: 71 passed. Full offline suite: 878 passed in 531.317 seconds,
with no failures or skips after restoring the exact historical test fixtures.
The tests cover batch errors/platform flags, actual one-process/one-scan invocation,
consistent concurrent capture, late receipt visibility, source/checkpoint/auth
corruption, actual deadline crossing, B5 regression fixtures, immutable transition
binding, known historical genesis, financial equivalence and restart replay.
B4 crash/slot/confirmation coverage remains in the full regression suite.

`tests/benchmark_b6.py` is opt-in and uses a TemporaryDirectory only. It creates
36,000 synthetic candles, 149 observations and 149 receipts; no live DB is read.
`docs/live0b-b6-performance.json` preserves all measurements. Three before samples
measure the existing repeated-verification work (not the entire old consumer):
32 Git processes and eight full scans, median **17.7894 seconds**. Twenty after
samples each launch a fresh Python process, including interpreter startup and imports, and measure complete B6 empty-exposure invocations through the historical
root/transition/checkpoint and B4 reconciliation: one Git process, one full scan,
median **4.7832 seconds**, nearest-rank p95 **5.4056 seconds**, maximum
**5.7871 seconds**. All targets passed. Production timing under different load
and future large financial journals is not guaranteed by this fixture benchmark.

## Proposed cutover — separate approval required

1. Review/commit the isolated implementation and final identities. Do not merge
   or change the running checkout as part of this implementation task.
2. Select an operational window away from an acceptance deadline. LIVE-0B is already operator-disabled; keep it disabled and confirm no invocation
   remains running. LIVE-0A stays running. Do not kill a process or clear unresolved exposure.
3. Confirm no B consumer owns the lock. Reconcile with the old runtime, record
   task settings and obtain consistent backup copies of activation, operational
   DB and paper ledger. Preserve all evidence and initial equity availability.
4. Install the reviewed code with no dependency or task-definition change.
   LIVE-0A can remain running: its scoped source bytes/identity are unchanged.
5. Run the new read-only transition `plan(run_id, root=..., effective_ms=...,
   now_ms=...)`, choosing an explicitly approved future time at least five
   minutes ahead. Verify the source chain via a fresh read-only capture, and
   verify historical ledger replay/checkpoint. Do not consume observations.
6. Review the exact proposal/fingerprint and old/new identities. Under the
   consumer lock, call `bind(..., approved_fingerprint=...)` to create the one
   immutable transition. Never overwrite activation or a transition.
7. At/after the effective time, load normally and run read-only reconciliation
   and reporting. Check unchanged T0/rules/equity/slot and historical prefix.
8. Enable the existing unchanged PT1M/PT2M/IgnoreNew task only after approval.
   Let it execute autonomously; do not manually consume. Inspect subsequent
   duration, counts, warnings and deadline behavior. Missed windows stay missed.
9. On any failure, leave B disabled and preserve evidence for investigation.
   Never delete/reseal a binding or silently roll back authorization.

No cutover, transition activation, task change or live invocation was performed.

## Isolated historical test fixtures

Only the 38 frozen research files listed with exact source paths and SHA-256
hashes in `live0b-b6-historical-fixtures.json` were copied from production.
Independent selection/manifest anchors verified every file before copying.
Detached copies occupy the isolated worktree ignored `artifacts/research` paths
required by unchanged legacy tests; no links, forward databases, current
authorizations, transitions, tasks or mutable runtime files were copied.

Downtime observations remain subject to actual B2 acceptance clocks. Expired
actionable signals cannot be backdated or reconstructed into accepted trades.
No transition fingerprint for the live run is proposed before a fresh authorized
checkpoint and future effective time are reviewed.
