# LIVE-0B explicit artifact binding (isolated install preparation)

FORWARD_PAPER / NON_EXECUTING. Base 94e0128cd64dff7da22d3e1a1bc154ad89a9b934.
No production artifact is created by this implementation/validation phase.

## Authority lifecycle and limits

Proposal -> independent human review of exact fingerprint -> explicit administrative
bind -> canonical immutable bound artifact -> read-only trusted loader capability.
A proposal, matching hash, approval flag, or historical authorization value object grants
no runtime capability. ClockCapability and AbandonCapability public construction raises;
only the canonical loaders issue them. Runtime methods require the exact capability type,
run/binding match, canonical ledger directory, current bound bytes, and a valid effective
authorization chain. A capability from another deployment root cannot open this ledger.
Private Python internals are not a hostile-code sandbox. This protects the ordinary
application interfaces and execution paths, not an administrator replacing program or
store bytes, arbitrary in-process introspection, or an operator deliberately invoking
an administrative command without human permission. No signatures or keys are used.

## Artifact locations

All paths are relative to a verified repository root:

- Proposal: artifacts/forward-paper-review/<run>/clock-v3-successor-v1/<hash>.proposal.json
- Resolution proposal: artifacts/forward-paper-review/<run>/clock-abandonment-v1/<hash>.proposal.json
- Successor binding: artifacts/forward-paper-consumer/<run>/authorizations/clock-v3-successor-v1.json
- Exact resolution binding: artifacts/forward-paper-consumer/<run>/authorizations/abandonments/<reservation>.json

Runtime loaders derive paths from run identity (plus exact reservation for resolution).
They do not accept proposal paths, bytes or claimed approvals. Redirected paths and
hard-linked files are rejected. activation.json and transition-b6-v1.json are not rewritten.

## Administration (commands require separate human authorization)

```
python -m paper_admin bind-clock-v3 --run-id <run> --reviewed-fingerprint <hash>
python -m paper_admin bind-abandonment --run-id <run> --reservation-id <id> --reviewed-fingerprint <hash>
python -m paper_admin resolve-abandonment --run-id <run> --reservation-id <id>
```

Read-only proposal builders and the exclusive review-artifact writer are administrative
functions. No automatic proposal or binding occurs from import, load or consume. The
paper command dispatcher has no bind/resolve command. Import-boundary tests ensure the
consumer and execution modules do not import paper_admin. Runtime source identity
includes the administrative source bytes without importing that module.

Bind acquires the existing consumer lock followed by the paper database lock. It reads
exact canonical reviewed proposal bytes, regenerates the proposal against reconciled
state/current identities, and requires exact equality. There is a five-minute minimum
future margin for successor binding. The margin is checked again after validation and
before destination creation; clock regression or an expired margin leaves no bound file.
The exact five-minute margin is inclusive. This is a sampled-time check, not an OS-level
atomic clock/filesystem transaction. A resolution binds one exact BLOCKED_CLOCK owner,
not generic future abandonments. Bind itself does not resolve anything. Resolution is
a separate explicit command under consumer ownership, with the ledger acquiring its
own lock and rechecking the exact checkpoint inside its transaction.

Any existing bound destination is refused, including an identical second bind. A failed
or partial binding is not overwritten or automatically repaired. Proposal and artifact
hashes use existing canonical JSON/Decimal conventions; no new cryptographic primitive
or dependency is introduced.

## Actual persistence guarantees

The writer uses Python open(mode='xb') to request exclusive creation. It writes canonical
UTF-8 JSON plus one LF, flushes the Python stream and calls os.fsync on the file descriptor,
then rereads/verifies the artifact while holding ownership locks. On the supported local
Windows filesystem this relies on exclusive-create semantics and the OS implementation
of file flushing. It is not a cryptographic approval, a guarantee against administrator
replacement, or a promise that hardware survives every power failure. Parent-directory
metadata is not separately fsynced. No claim of atomic file visibility is made: a crash
can leave a partial file. Such a file fails canonical parsing/loading and blocks reuse.
SQLite transaction/locking guarantees are separate; authorization binding does not edit
the financial database. OS power-loss and abrupt-process-kill testing are not claimed.

## Successor schema and dispatch

The successor binds original activation and B6 fingerprints, frozen old and current
implementation identities, unchanged T0/equity availability/config/rules/source/B2/B5
policies, clock policy/implementation/effective boundary, and a fresh reconciled genesis,
journal prefix, account and slot checkpoint. The predecessor B3/B4/B5 identities are
explicit historical constants; compatibility does not pretend the current tree equals
the historical tree. The original B6 envelope is verified independently.

The normal consumer uses load_execution only. Missing, malformed, premature or changed
bindings cannot select v2 as a fallback. Existing boundaries are skipped/returned without
resampling. New prospective reservations require S >= the successor effective 4H boundary
and actual recorded acceptance >= effectiveness, in addition to S > original T0. Earlier
aligned boundaries encountered during catch-up are already past their unchanged deadline;
they can record existing missed/NONE dispositions but cannot create a reservation.
D=S+14m and E=S+15m remain fixed. Both v3 UTC samples must be <=D inclusively.
CONFIRM persistence latency cannot change the preserved post-ACCEPT sample.

B6 source capture remains invocation-scoped and detached, exactly once. There is no
cross-invocation trust cache. Authorization guards reread bound bytes and current identities;
B4 financial verification remains independent. Invocation budget still starts before
verification. Cadence PT1M, timeout PT2M and IgnoreNew are unchanged.

## Exact abandonment and replay

The resolution additionally binds the successor binding ID, exact original reservation,
ACCEPT/CONFIRM payload/event identities, journal head, account/slot, mechanism identity,
resolution timestamp and NO_ENTRY/NO_REPLAY/NO_FALLBACK/NO_FINANCIAL_EFFECT constraints.
The executor checks for matching loader/ledger ownership and uses the existing atomic
journal -> ABANDONED projection -> slot-release transaction. Original events remain
unchanged. An identical retry finds the one existing event even after later legitimate
journal growth. Conflicting retry or changed bound bytes fails closed.

Journal events embed complete bound approval evidence. Historical verification is a
pure data interface; it does not inspect current bound files or issue execution authority.
Recorded v2 remains v2. v3 calculation dispatch remains tied to its recorded version and
frozen policy; future algorithm changes must add another version rather than reinterpret
this one. Historical implementation/mechanism fingerprints are recorded provenance, not
required to equal today's source hashes. Runtime loader checks are separately strict.
Deleting current authority prevents new execution but leaves intact historical replay.
The existing BTC incident is not resolved or reinterpreted by this code installation prep.

## Tests and residual scope

Tests use temporary synthetic run roots only. Older B5 financial regressions explicitly
mock a historical v2 loader in test setup; production has no such fallback. New integration
tests exercise the real binder and canonical loaders. Recovery/crash fixtures use real
bindings and retain rollback, schema-extension, no-financial-effect and idempotency checks.
No live database, proposal, authorization or source evidence is copied.

A future production proposal/bind/install/resolve/enable sequence still requires separate
human review. This commit is not permission to perform any of those operations.

## Historical interpretation

This install-prep supersedes the uninstalled capability API in candidate 94e0128.
The deployed v2/B6 journal format and its replay are unchanged. New v3/abandonment
events include the canonical binding envelopes as recorded historical evidence.
Tests prove replay does not need current bound files and cannot issue execution
authority. Existing production evidence was not read, copied, or resolved for this
validation; the BTC clock incident is checked using its recorded timestamp values
and synthetic historical v2 journals. No claim of a fresh live database audit is made.

## Validation identities and measured performance

Python 3.12.10, existing project runtime; no dependency changes.

- Focused artifact binding: 28 passed.
- Recovery / v3-ledger / historical-schema regression: 22 passed.
- Targeted import-isolation check: passed.
- Fresh Python processes: 10; synthetic 36,000 candles and 149 observations.
- p50 3.924032s; nearest-rank p95/max 4.255451s.
- Every process: one git cat-file --batch, one full source scan, CREATE_NO_WINDOW
  (134217728). All completed WAITING with zero transitions.
- This measures the representative waiting path, not an arbitrarily large future
  financial journal or production latency. No live consumer was run.

Deterministic fingerprints:

| Component | Fingerprint |
|---|---|
| Successor schema | 83f0315b5508e472c5ae704011a7784e8992183666e9218b1f3f38327af4b46d |
| Abandonment schema | abeecb31989fde9340a1e563998064a7e5bfd02e1e7db8dbf70f17ec93365848 |
| Administrative binding | 95cef1864832d32a139b38a5712cd048160015564b575f0c75896071b8648646 |
| Execution loader | 5dee0daea47535312fe09475ef0077874babee024952a8590e5fbc6c19586bf2 |
| B3 | 2d85a3ab729a51e4c94a0b20384e008268707b563dfc52f90c07956145854027 |
| B4 | f5331a809b36c25ca7ab5c80d02661d40bd8ac2be2cb8a54c5e7a722f859530b |
| Runtime | 262f4c92ee6951da11fb3d783735b2593a03794140bb8863bb964684da62dcbc |
| Clock-v3 policy | 6e980cd5b434371123e1aeeb3a674f5daf6fa83125b0422e16893f7eedf7a7d4 |
| Clock-v3 implementation | 0f30a749a786de6138317f2853044448c9145dd9dc2b091ff7ff8d8af0bd9792 |
| Abandonment mechanism | 862083825dbb61ef7c8e6f6f84341583ff034046a5887d491cc283e4e4033a7c |

All 38 existing frozen research fixture hashes remain unchanged. Fixed-revision
historical B3/B4/B5 identities independently reproduce as ae8493a... / 0c3543da... /
c27c64c7... respectively. Existing activation/B6 artifacts are not reissued.

Final complete offline suite: **942 passed in 418.726 seconds**, including 71
B6 regression cases and 14 clock-rule tests. Syntax/import-isolation/diff checks pass.
The preceding 942-test run found only a missing explicit paper.operations entry in
the ledger import allowlist for the canonical directory guard. That narrow test
allowlist correction passed independently and the complete final rerun is green.
No runtime source changed after the focused 28/22 passing runs or benchmark.

The bind-time regression tests reject margin expiry (including effective time crossed
during validation) and UTC regression before creating a destination. Exact five-minute
margin passes. Cross-deployment-root capability misuse is rejected before ledger writes.
