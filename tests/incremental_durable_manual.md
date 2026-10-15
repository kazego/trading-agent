# Stage A — manual Windows synthetic validation

Prepared, NOT executed outside the sandbox. No live consumer, source adapter,
activation, runtime database or real authorization is used. Stage B is not
implemented. The only supported root is exactly:
`C:\Users\diego\AppData\Local\TradingAgentSyntheticTests`.

## Command (ordinary PowerShell; not Administrator)

```powershell
Set-Location -LiteralPath 'C:\dev\trading-agent'
& '.\.venv\Scripts\python.exe' -I -B '.\tests\run_incremental_durable.py' --root 'C:\Users\diego\AppData\Local\TradingAgentSyntheticTests'
$LASTEXITCODE
```

Do not substitute a live path. The pre-existing authorized parent must resolve
strictly without links/junctions. Each case allocates a new UUID directory using
exclusive creation. The parent root is not created by the runner. Nothing is
automatically deleted; no permissions, tasks or configuration are changed.
An error aborts the matrix; do not repair/reuse a failed case. A new invocation
creates new cases and preserves the old evidence.

Expected final output: JSON status PASS and an evidence directory. That directory
contains worker-NNNN.json (exact command, exit status, stdout, stderr) and
completed-cases.json. Preserve those files plus terminal output for review.
Each worker has a 180-second limit; the whole matrix has a 1800-second deadline.
The remaining global budget may stop a worker sooner. Timeout logs include command,
duration, partial output and FAIL; subprocess.run kills/reaps the direct child
before raising TimeoutExpired. Workers cannot launch descendants. Python process
creation/kernel stalls and forced termination of the parent itself are not a
hard-real-time guarantee; no Windows Job Object is claimed. The matrix also writes
FAIL.json on an exception. Do not interpret completed-cases.json alone as PASS.

Exit77 is an expected injected abrupt exit ONLY when the parent expects it;
exit23 means the worker rejected input/state; any unexpected exit fails the run.
No tests are currently certified to pass on the manual Windows environment.

## Strict serialization review

- Markers/case bindings: strings, hashes only; strict encoded().
- Synthetic fixture identity/evidence/journal, financial reducer and compact
  continuity: integers, Decimal values/canonical strings, booleans/null and their
  containers; strict encoded()/fingerprint() unchanged. Financial floats fail.
- Persisted audit metadata: time.time() returns floating-point seconds. Only its
  `at` field is encoded as `{encoding: seconds-binary64-hex-v1, value: float.hex()}`.
  Reopening restores precisely the original binary64 sample, not a rounded epoch
  or an extended validity window. Integer synthetic/legacy seconds remain integers
  with unchanged bytes. Nonfinite, negative, unknown/noncanonical timestamp
  encodings are rejected. This is an exact representation of the sampled value,
  not a claim of greater physical clock accuracy. Policy remains 24h/1000 events.
- Worker duration/timeout reports: dedicated telemetry JSON, finite floats only
  in duration_s/timeout_s. No financial fields accepted by that report encoder.
- FAIL/completed-case reports: strings/integers/lists, strict encoded(). Worker
  stdout is diagnostic JSON with string equity and integer sequence, no audit time.
- Report-write failures attach notes to an existing worker/test exception rather
  than masking it. If there is no prior failure, a report-write failure still
  fails the run. If disk reporting is unavailable, preserve the terminal traceback
  and notes; no implementation can guarantee a file report on an unwritable disk.

The failed manual-run evidence is never converted, repaired or reused. Future
manual validation always starts in newly allocated synthetic case directories.

## Design and test inventory

- Same deterministic Replay financial reducer, compact state and SQL transaction
  as the memory prototype. The default memory core STILL rejects disk databases.
  A research-only subclass accepts only the exact owned synthetic file.
- Lifetime nonblocking msvcrt byte-range ownership across DB open, recovery,
  COMMIT, continuity publication, audit and DB close. Nested core locks are
  in-process only; the outer OS lock provides cross-process exclusion. All
  cooperating writers must use it. Never delete lock files to reclaim ownership.
- SQLite DELETE journal mode, synchronous FULL, foreign keys enabled. No backup
  or deserialization of any operational database: fixture starts from memory.
- External full envelopes carry sequence, financial state, head and identities;
  records include explicit value_hash and previous-record linkage, and a separate
  checksum receipt. Each is create-once and fsynced. Reopen validates canonical
  bytes, contiguous filenames, linkage, checksums and sequences. No tail locator.
- Partial record or missing receipt blocks. There is no automatic overwrite,
  truncation or completion of partial records. One committed event with no
  publication can be replayed, evidence-checked and financially checked against
  the previous anchor. Larger unexplained gaps and DB rollback block.
- Independent audit records persist append-only. Missing/expired verdict blocks
  financial transitions; 24h/1000-event policy is unchanged. Full audits run here
  as test verification, not a nonblocking production snapshot implementation.
- Cases cover complete reopen/retry, six abrupt process exits around journal/
  projections/state/COMMIT/publication, three publication interruption points,
  interrupted recovery, financial corruption, external corruption, old DB,
  old external prefix with two unpublished events, competing process ownership
  and release. Every successful worker performs full replay reconciliation and
  SQLite integrity_check. These are small BAR fixtures with an open SHORT;
  broad financial scenarios remain covered by the memory suite, not yet by this
  disk matrix.

## Isolation and trust limits

Every worker installs its own audit hook. Disk SQLite and data reads/writes/scans
are restricted to its one assigned case; trusted Python source/library reads
are separately allowed. A child cannot access sibling cases or older runs.
The parent retains access only to the suite and cases it allocated this time.
Artifact reads, networking, shell execution, deletion, renaming and permission
changes are rejected. The parent checks the entire command, executable, cwd and
environment; workers cannot launch children. Isolated Python mode ignores Python
environment/user-site overrides; the installed interpreter remains trusted.

There is no caller-selectable --worker path/action interface. The parent maintains
an in-memory case registry and sends a random per-case capability through stdin,
bound to suite/case/action/fault. The immutable case-binding.json contains only
the capability digest. The raw capability is neither logged nor stored on disk.
The child reads only fixed binding metadata before validating membership and
installing the case-specific hook; no fixture is opened before validation.
The ordinary marker alone grants no worker access. Never invoke internal worker
mode manually. This is ephemeral test-process isolation, not production signing
or a new financial authorization scheme.

Guards are supplementary, not an OS
security boundary against arbitrary native code or a malicious administrator.
The root and its contents must remain exclusively controlled by the tester;
strict resolution and pre-open checks alone do not eliminate hostile TOCTOU.
No signatures or new real authorization trust model is introduced.

The runner deliberately appends corruption to newly generated test records and
restores an older synthetic DB through SQLite in selected fault cases. It never
overwrites/deletes files from previous runs. Those intentional mutations are
test inputs, not repairs. All evidence is preserved.

## Durability claims that remain unproven

flush/fsync requests OS flushing; SQLite FULL provides its documented commit
protocol subject to OS/filesystem/device behavior. Python does not establish
durable directory-entry publication or honest device caches. os._exit simulates
process interruption, NOT sudden power loss, disk loss or a torn hardware write
during COMMIT. An actual power-loss test needs separate authorization/environment.

Checksums stored beside state are not a trust root. Initial trust comes from full
bootstrap verification and exclusive ownership of separate continuity history.
Independent corruption/rollback is checked; coordinated rollback or replacement
of DB, continuity, audit store and code is not defended. Replacing all external
records with an old prefix one event behind is indistinguishable from a legitimate
unpublished commit: only exact verified replay is allowed, never a new financial
effect. Authentic historical rollback detection needs a separately protected
monotonic witness; this prototype does not claim one.

Continuity lookup scans its files linearly. No performance/scalability claim and
no 70000 matrix are included. Full snapshot audit, publication to a moving live
head, production loader integration and migration remain outside Stage A.
