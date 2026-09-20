# LIVE-0A.1 — FORWARD_PAPER / NON_EXECUTING

Operational implementation only. No poll, market-data acquisition, migration
activation, environment installation, scheduler registration or enabling is part
of this change. The existing live-0a-001 manifest, database and observations are
preserved. Signal/risk/context kernels and Semantic Contract V2 are unchanged.

## Explicit migration, not a replacement manifest

`operations-v1.json` is an exclusive, immutable sidecar written only by an explicit
`bind-operations` command with both an approved implementation hash and approved
plan hash. It binds the original manifest, current scoped implementation/software,
timing/recovery policy, explicit future UTC 4h effective boundary, and checkpoints
of the existing observations, candles and events. It never updates the original
manifest or existing database rows. The effective boundary must be at least five
minutes ahead at binding; no automatic date selection or movement is allowed.

The legacy manifest fingerprint remains:
`a36d985436303dea4d477e76718cefa9ec6d3155df74d98e2850efcd0139bf6b`.
Its recorded implementation and two-minute policy remain historical evidence.
The legacy specification is verified exactly, and numerical/context/data modules
are checked against Git commit `9880a91f6fef772a69e696e3bee77cb40e151d9c`.
Current operational sources have a separately approved implementation identity.
This is not a directory-wide source exclusion or a bypass of source verification.

Read-only commands can inspect the recognized legacy run without activation.
An upgraded poll cannot use it until the operational binding is approved.
Do not invoke poll as a preflight: a failed execution authorization check latches
BLOCKED. Use the read-only `status` and `plan-operations` commands instead.

The existing 2026-09-12 00:00Z and 04:00Z observations keep their original hashes
and DELAYED_RECONSTRUCTED labels. Future prefix verification permits appending
new evidence but detects changes/removal in the checkpointed historical rows.
Run manifests and hashes protect against accidental corruption; they are not
signatures against an administrator who can replace all files and hashes.

## Timing

Target polls are :03/:18/:33/:48 UTC, three minutes after each 15m close.
Strategy decisions remain limited to completed UTC 4h boundaries, strictly after
T0. Operational changes never introduce another signal timeframe or entry logic.

At/after the approved effective boundary T, ONLINE means the post-commit timestamp
is no later than T + 300,000 ms, inclusive. Later observations are
DELAYED_RECONSTRUCTED. Newly recovered boundaries before the effective boundary
retain the original 120,000 ms tolerance. Existing observations are never relabeled.

The clock anchors public server time to monotonic time with a conservative full
request-round-trip allowance. Time requests over ten seconds, backward monotonic
time, local-wall/monotonic disagreement over 30 seconds, or a subsequent public
time check differing by over 30 seconds fail closed. No time is automatically fixed.

Each new observation is committed with decision-ready time and `UNCONFIRMED` timing.
Only AFTER that SQLite FULL-synchronous commit does the observer sample the clock
and append a durable RECEIPT event containing its observation hash, upper-bound
persistence time, applied tolerance, operational hash and final classification.
Read commands show `effective_timing` from the receipt while retaining the original
row unchanged. The event commit is separate; a crash in the gap cannot cause a
false timely claim. A missing receipt remains UNCONFIRMED until the next authorized
poll adds a conservative DELAYED_RECONSTRUCTED recovery receipt with
COMMIT_TIME_UNKNOWN. It does not regenerate the signal or rewrite the observation.

## Recovery

Only recognized connection/timeout/interrupted-read failures and HTTP
408/429/500/502/503/504 are recoverable. The observer writes a timestamped
RECOVERABLE event and exits cleanly. It never retries inside that invocation.
The next scheduled call may retry, subject to persisted Retry-After backoff.
Numeric Retry-After seconds are supported; unsupported/malformed header forms
fail closed instead of guessing. A 429 without a header waits at least 15 minutes.
No endpoint fallback, credentials, SDK or authenticated request is introduced.

Certificate errors, unknown response states, invalid data, changed fingerprints,
clock disagreement, chronology/revision conflicts and database failures are
critical. A fsynced exclusive BLOCKED.json sidecar latches the state even if the
database cannot be opened; a database event is also written where possible.
Subsequent invocations refuse network access and never clear the latch. If the
filesystem itself cannot persist the latch, the process fails; no durability claim
can be made under total storage failure. A second writer exits BUSY without polling.

Existing tables, atomic candle pages, global boundary primary key and OS ownership
lock remain in use. START/STOP events now carry timestamps. Old events without
timestamps are preserved; an old last-success time is shown as unknown, not inferred
from candle close time. Only the public time and kline GET endpoints are permitted.

## Read-only commands

```powershell
python -m forward status --run-id live-0a-001
python -m forward latest --run-id live-0a-001
python -m forward summary --run-id live-0a-001
```

All return canonical JSON labeled FORWARD_PAPER / NON_EXECUTING. They open SQLite
with `mode=ro`, `query_only=ON` and a consistent read transaction. They never construct
a writable Store, fetch data, evaluate signals, repair or create a block latch.

- status: health, freshness, last timestamped successful poll, per-symbol/common
  candle boundaries, next unrecorded 4h boundary, count, policy and warnings.
- latest: most recent persisted observation, BTC/ETH/ADA signal/ATR/context and
  original/effective timing plus receipt if present.
- summary: observations and symbol decisions since T0, LONG/SHORT/NONE counts,
  timing totals separated by operational identity and tolerance, and overdue count.

Missing/corrupt or unauthorized local state produces a sanitized verification
failure, never partial trusted financial output or an automatic state change.

## Stable Windows runtime — prerequisite, not installed

No project `.venv` or standalone interpreter was found in the checked command,
Python registry and standard installation locations. The development-test runtime
is Python 3.12.14, pandas 3.0.1 and numpy 2.3.5 in the Codex cache. It is not the
scheduled executable. No environment/dependency was installed or changed.

Proposed task executable:
`C:\dev\trading-agent\.venv\Scripts\pythonw.exe`.

Before activation, separately approve a stable standalone Python installation and
project-owned venv with the reviewed package set. Prefer matching the tested Python,
pandas and numpy versions; preserve/pin their transitive dependencies after review.
Do not blindly install the unpinned project requirements to establish this runtime.
The observer does not require python-binance. Do not make the venv depend on a
removable Codex cache base. A differing runtime changes implementation identity and
requires revalidation and a new approved operational plan before binding.

## Windows task configuration

Script: `scripts/live0a-task.ps1`. Default Generate creates a new XML file only.
RegisterDisabled is a separate explicit action that requires the stable executable,
a passing operational binding check, an exact matching reviewed XML, and no existing
task with the same name. No enable action is supplied.

- Name: TradingAgent-LIVE-0A-live-0a-001.
- Project .venv pythonw.exe, working directory C:\dev\trading-agent.
- Arguments: `-m forward poll --run-id live-0a-001 --public-data-only`.
- UTC explicit future :03/:18/:33/:48 start; PT15M repetition without expiry.
- IgnoreNew, PT10M execution limit, StartWhenAvailable; no RestartOnFailure.
- InteractiveToken, LeastPrivilege; run only when the current user is logged on.
- No idle requirement; wake where supported; task Enabled=false in generated XML.

Locked sessions can run it; logged-out/powered-off machines cannot. Missed starts
may run late and will not be labeled timely merely because a scheduler launched
them. The script never enables a task or changes a Windows execution policy.
The commands below use RemoteSigned for that PowerShell process only, not a
persistent policy change. Local unsigned scripts are acceptable under RemoteSigned;
domain-enforced restrictions must be resolved by the operator, not bypassed.

After selecting fresh explicit future timestamps (not executed here):

```powershell
python -m forward plan-operations --run-id live-0a-001 --effective-boundary <UTC-4H-TIMESTAMP>
python -m forward bind-operations --run-id live-0a-001 --effective-boundary <SAME-TIMESTAMP> --approved-implementation <HASH> --approved-plan <HASH>
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\live0a-task.ps1 -Action Generate -StartBoundary <UTC-POLL-TIMESTAMP> -OutputPath <NEW-XML-PATH>
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\live0a-task.ps1 -Action RegisterDisabled -StartBoundary <SAME-POLL-TIMESTAMP> -OutputPath <SAME-XML-PATH>
```

Plan and Generate are review steps, not activation. Binding, registration and
enabling each require their own explicit authorization. Implementation/tests do
not perform any of them on the real run.
