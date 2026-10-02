# LIVE-0B clock-confirmation-v3 and explicit abandonment

Isolated implementation based on `1800875f40158c0fbfbff2d221a907a51bd8dfe2`.
FORWARD_PAPER / NON_EXECUTING. No production deployment or resolution is performed.

## Clock contract

Capture each sample as perf_counter_ns -> time.time_ns() // 1_000_000 -> perf_counter_ns.
The supported runtime profile is GetSystemTimeAsFileTime (15.625ms resolution) and
QueryPerformanceCounter (100ns resolution). Other profiles fail closed before acceptance.
qU=15,625,000ns; qP=100ns; qT=1,000,000ns; U=2*(qU+qT)=32,250,000ns.
Each bracket must be <=5,000,000ns. This is an engineering measurement allowance,
not a claim of UTC accuracy. There is no rate allowance and no five-second interval cap.

For acceptance triple A and post-ACCEPT-commit triple C:

```
delta = (C.utc_ms - A.utc_ms) * 1_000_000
low = C.before_ns - A.after_ns - 2*qP
high = C.after_ns - A.before_ns + 2*qP
consistent = delta + U >= low and delta - U <= high
```

Require valid nonnegative integer samples, A.before <= A.after <= C.before <= C.after,
no recorded UTC regression, and bounded brackets. Violations produce BLOCKED_CLOCK.
Consistency does not extend the deadline: recorded A.utc_ms and C.utc_ms must both
be <= D=S+14m, inclusively. Otherwise confirmation is MISSED_ACCEPTANCE_DEADLINE.
E remains S+15m. CONFIRM persistence may finish after D using the preserved timely C
sample, without resampling. All triples, profile/policy, interval, allowance, deadline
and result are journaled. A failed acceptance sample creates no acceptance; interrupted
ACCEPT remains unconfirmed and cannot be upgraded by a later retry.

Legacy CONFIRM and v2 confirmations retain their original algorithm. The synthetic
reproduction of the incident (acceptance 1790870648261, durable 1790870648645,
390000000ns elapsed) remains BLOCKED_CLOCK under v2, including replay.

## Explicit authorization and isolation

Ledger.accept_v3 requires a canonical ClockAuthorization binding the ledger binding ID,
effective timestamp, exact clock implementation and policy and an externally approved
fingerprint. This is an explicit approval capability, not a digital signature or a
self-service production authorization. The existing consumer continues to call v2.
No default policy changes, activation writer, CLI activation or live transition is added.
A future reviewed deployment must bind these identities and explicitly select v3;
existing activation and B6 transition cannot authorize these changed source identities.

## Append-only resolution

RecoveryAuthorization binds exact genesis, journal head, account, slot, reservation,
original ACCEPT/CONFIRM event IDs and payload fingerprints, rules/binding, mechanism,
resolution timestamp and operator reason. It requires an independently approved hash.
Only the exact BLOCKED_CLOCK owner without financial children can become ABANDONED.
No current production reservation ID is hard-coded.

Under exclusive ledger ownership, one BEGIN IMMEDIATE transaction verifies history,
appends ABANDON_BLOCKED_CLOCK, materializes the terminal reservation and resolution,
releases the slot, verifies replay/foreign keys, then commits. No intermediate state is
visible. No fills, positions, trades, fees or equity events are created. Original ACCEPT
and CONFIRM journal bytes remain unchanged; effective readout is ABANDONED while the
original confirmation remains BLOCKED_CLOCK. Historical boundary arbitration is never
repeated; lower-priority candidates remain blocked.

Legacy reservations CHECK constraints require a transactional schema extension at the
explicit resolution only. The exact known legacy schema is checked, rows are copied
without changing payloads, the unique exposure index is restored, and foreign_key_check
runs before commit. FK enforcement is restored even on failure. No schema mutation is
needed for read-only legacy reconciliation. A new resolutions table disallows UPDATE
and DELETE. Identical retries return the same event; conflicting retries fail closed.
Crashes before commit roll back journal/schema/materializations; after commit replay
recovers the same terminal state and empty slot.

## Validation and historical preservation

Focused tests exercise both disagreement directions at 0/1/5/6/10ms, envelope +/-1ns,
50ms, regressions, invalid samples, 5ms bracket edges, inclusive deadlines, delayed
CONFIRM persistence, unsupported profiles and v2 incident replay. Recovery tests cover
wrong approvals/checkpoints/owners, financial contradictions, all transaction fault
points, schema rebuild rollback, restart/idempotency, and closed-trade preservation.
Historical B6 activation/genesis tests reconstruct original B3 bytes from fixed-revision
Git blobs rather than substituting today's implementation. Import allowlists admit only
the new pure modules. No dependency, strategy, sizing, slippage, fee, or scheduler change.

The 38 frozen historical research fixtures listed in live0b-b6-historical-fixtures.json
were copied from the prior isolated B6 worktree and hash-verified. No live forward data,
authorization or database was copied. Historical identities remain reproducible:

- B3: ae8493a638ee9017df9641a12bbe910fd1d0cde0572ff907df99e1c2cb056d94
- B4: 0c3543daf8499baaaf969a76f077f7dd6a34accdbe9cf31b17c3b4fb6e4cde78
- B5: c27c64c7ff65b70e7493009f902fa1d62e02f4183af4e292e1b817804c48efc5

Current isolated identities:

- Clock policy: 6e980cd5b434371123e1aeeb3a674f5daf6fa83125b0422e16893f7eedf7a7d4
- Clock implementation: 9e5e6f358ad0858e941bdd7101fa71be475560e7b455426c7ea209642ae230a8
- Abandonment mechanism: f0d5c7ad370a32da36733209519488c58835a2b2314e4855681d9833f86d3cad
- B3: 2d85a3ab729a51e4c94a0b20384e008268707b563dfc52f90c07956145854027
- B4: 4e23cfae09bb13fe650d73cbf534bd32bd34d843f1d9cc9bbbfdcf71d63ffb76
- B5/runtime: 9bf61b21404ff80b7fc9bc01a09ffd98bf7757c06f06f38c411a05374c9e4939

Production cutover, future authorization binding, actual abandonment and task enablement
require separate review/authorization. LIVE-0A console-window work remains excluded.

## Completed validation

- New focused clock/recovery tests: 36 passed (14 clock + 22 recovery).
- Canonical behavioral contract: 15 passed independently.
- Full offline suite: 914 passed in 336.784 seconds.
- Syntax, pure-module import isolation and git diff --check: PASS.
- All 38 frozen historical fixture hashes verified unchanged after validation.

The first full run exposed Windows checkout CRLF conversion in the canonical contract
JSON. The isolated copy was restored to exact committed LF bytes; no JSON content or
historical fingerprint changed. The final full suite passed. No dependency change.
