# Incremental core: deferred filesystem validation

Status: design/procedure only. No filesystem continuity backend or worker CLI is
implemented by this document. No command below authorizes execution outside the
sandbox. Never point these tests at a real Trading Agent database or artifact.

## What the memory benchmark measures

`benchmark_incremental_memory.py` uses only `:memory:` SQLite and ExternalMemory.
It retains the original `Replay.apply`, financial formulas, full bootstrap and
full final audit. Every timed sample constructs a fresh SyntheticLedger object,
verifies local continuation, processes the exact next BAR, commits SQLite in
memory and publishes into the memory continuity store. Python/process startup,
production authorization loaders, LIVE-0A capture, filesystem locks, fsync and
power-loss durability are excluded. Replays/fixture preparation are separately
reported. Samples advance N through N+30, not thirty copies of an unchanged N.
The workload is 66 ACCEPT, one CONFIRM, one ENTRY and N-68 historical BAR events,
with one open SHORT and 222 additional synthetic bars. This is one workload,
not a distribution of closed trades or symbols.

Inclusive nested stage times overlap; never sum them. Exclusive stage time
subtracts nested instrumentation. Wall-clock total includes instrumentation and
the small wrapper/data setup overhead. No identity check or formula is patched
out. Only a counting wrapper around Replay.apply reports long preparation/audit
progress. Thirty observations are an empirical p95, not a strong tail estimate.

## Static finding: strict path resolution

SourceAdapter (`paper/source.py`) and CandleSource (`paper/market.py`) call
Path.resolve(strict=True) in their constructors. This is existing runtime
behavior, not only a benchmark assertion: it requires an existing resolved
source directory and normalizes symbolic links/junctions. Binding also checks
canonical paths, symlinks and link counts. Strict resolution alone does not prove
containment, authorization, or absence of a later path replacement (TOCTOU).

The old stage harness additionally uses resolved-path containment for audit
hooks and cleanup. The memory core does not instantiate SourceAdapter or
CandleSource and has no need to resolve a database pathname: it rejects every
non-memory SQLite database. Its use of in-memory evidence is an explicit test
boundary, not a replacement for the production filesystem controls.

The prior WinError 5 proves that resolution of the probed directories failed;
it does NOT prove that Windows universally prohibits GetFinalPathNameByHandleW.
Static inspection found an additional confounder: the probes used mkdtemp,
whereas the existing allocate_root helper documents a Windows restricted-token
interaction with mkdir(0o700) and uses ordinary mkdir with inherited permissions.
No new filesystem experiment is run here, and neither cause nor a cure is claimed.
Do not remove strict resolution, chmod/chown files, alter ACLs, or use a different
API just to evade the failure. Confirm the actual environment with separate
approval before running any filesystem tests.

## Minimum authorized environment (future)

- Separate disposable checkout containing source/tests only, no artifacts,
  activation, live SQLite, credentials or scheduler definitions.
- Standard non-administrator account; no access to operational data. The approved
  interpreter and standard library suffice; no installation or new dependencies.
- One uniquely allocated private local temporary root, with normal inherited
  permissions. Existing paths must pass strict resolution and containment checks.
- Local filesystem with tested SQLite locking, exclusive file creation,
  flush/fsync and OS file-lock semantics; do not use network shares.
- Child processes execute only the audited test worker and receive only the
  synthetic root/token. Each child installs its own restrictions: Python audit
  hooks are not automatically inherited by another interpreter.
- Network and arbitrary subprocess launch denied by the environment. An audit
  hook is supplementary, not a boundary against arbitrary native code/admin.
- No access to real run IDs/paths; children reject aliased roots, attachments,
  arbitrary SQLite URIs, pre-existing roots and caller-supplied production paths.

## Durable protocol to implement/test later

Use one OS-level ownership lock across database recovery, transition COMMIT and
continuity publication. Never delete a lock file to reclaim ownership. Two
writers must not use independent locks for the same resolved synthetic database.

Continuity records must be create-once, canonically framed and checksummed,
sequence/parent-linked, and include run/genesis/schema/reducer/authorization,
journal head/count and compact state. A last-record locator is not a trust root:
test locator rollback, omission and conflicting records. A full linear directory
scan on every start must not be silently substituted for bounded startup.

Flush the complete record and call fsync; check read-back. Document that file
fsync, directory entry durability, atomic publication and device power-loss
guarantees differ across OS/filesystems. Do not claim a physical durability
guarantee from successful Python flush alone. Partial/malformed files fail closed
and are preserved, not overwritten or automatically repaired.

Only an independently verified bootstrap can establish the first trusted anchor.
After a commit-publication interruption, validate exactly one suffix event from
the last anchor against SQLite and its financial delta; publish only that result.
More than one unexplained event, missing DB commit, altered checkpoint or changed
authorization blocks. A coordinated administrator rollback of all stores is
outside this model; hashes do not prevent it.

Persist audit verdict/sequence/time independently. Missing, expired (provisional
24h/1000-event limits), regressed-clock or divergent audit blocks new transitions.
An audit cannot declare a newer journal prefix verified than it actually read.

## Required process/IO failure matrix

1. Bootstrap, close every handle, exit Python; reopen in a new process and verify.
2. Exit before BEGIN, after journal write, after financial delta, after compact
   state write and before COMMIT: old state or complete new state, never partial.
3. Exit after COMMIT but before publication: exactly one recoverable suffix;
   retry must not add a second event, fill, fee or equity application.
4. Interrupt external record creation after header/body/trailer, before/after
   fsync and locator publication. Partial records block, originals preserved.
5. Restore older DB only, continuity only, audit only; truncate/corrupt any one;
   test valid-looking self-hashes against an unchanged independent anchor.
6. Two synchronized workers contend for ownership before recovery and in the
   COMMIT-publication gap. Only one may advance; no forced unlock or termination
   of an operational process. Test OS release after the test writer exits.
7. Identical/conflicting retries after reopening, closed-trade retry, missing
   candle, dual touch, gap exits, audit expiry/count boundary and wrong ancestry.
8. Full historical audit compares every event/projection and detects old-row
   corruption outside the active working set. Detecting this corruption is
   periodic, unlike the immediate checks of active rows/new transitions.

## Reproducible execution order once tools/environment are approved

The following worker interfaces are specifications, not existing executable
commands. Do not run guessed module names. Implement and review the test backend
and its worker first, then freeze their hashes and exact commands.

1. Record source hashes/interpreter/platform. Allocate a unique synthetic root.
2. Preflight strict resolution, create-exclusive, read, fsync, close/reopen and
   contained cleanup; stop on any permission/alias error, without escalation.
3. Run minimal SQLite commit/reopen and OS-lock contention tests in two workers.
4. Run the above failure matrix, saving outcomes and preserved failed fixtures.
5. Run the existing core differential suite against the durable backend.
6. Run 30 fresh-process samples per history size, separately recording preparation,
   complete audits, process startup, core, disk COMMIT and publication. Include
   historical-continuity lookup cost; never omit it from the total.
7. Verify financial/journal equivalence and hashes, close all handles, then remove
   only the exact resolved disposable root. A cleanup failure is reported; do
   not alter permissions or recursively delete a guessed path.

No production migration, real authorization binding, task manipulation or live
consumer execution is part of this procedure. Memory benchmark success does not
authorize the filesystem procedure or certify any of its guarantees.

## Commands available now (memory only)

From the reviewed source checkout, using its existing interpreter:

```powershell
.venv\Scripts\python.exe -B -c "import sys,unittest;sys.path.insert(0,'tests');r=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName('test_incremental_benchmark'));sys.exit(not r.wasSuccessful())"
.venv\Scripts\python.exe -B tests/benchmark_incremental_memory.py --sizes 348 700 7000 70000 --samples 30
```

Stdout may be preserved by the invoking shell in a new research/test report file.
The Python hook denies writes, non-memory SQLite, subprocesses and network calls;
it does not pretend to govern the parent shell. No test child is launched.
The hook's lexical rejection of artifact reads is supplemental to the sandbox:
it is not an alias-proof general filesystem access control. The driver has no
caller-provided file/database arguments and uses only known source imports.
