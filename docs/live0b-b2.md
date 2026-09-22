# LIVE-0B B2 — acceptance and pending reservations only

**FORWARD_PAPER / NON_EXECUTING**. No activation, selected real start boundary,
network access, scheduler, fills, sizing, protection execution, positions, trades
or accounting is implemented. All tests use synthetic evidence/temporary stores.

## Preserved baseline and binding

B1's four implementation files are unchanged. Its configuration fingerprint is
`ae3108104de0005f9a532d5e447dcc816b8eaf2746961f4f77081ca180560337`; its historical
implementation/runtime fingerprint remains
`f3f5e761bcbda57ff75dfb2d514bfc17cd8a54ff57dc79e7e36e5bd4e8aab353` on the approved
stable runtime. B2's scoped implementation identity binds that B1 identity, the
two B2 modules and the explicit acceptance policy. No historical identity is
rewritten or waived. LIVE-0A authorization and its read-only adapter are unchanged.

`AcceptanceBinding` binds the immutable B1 draft, B2 implementation/policy and a
`ProspectiveGate`. The gate defaults to unset. It is an input capability for a
future externally authorized activation, not an activation API or artifact.
Synthetic tests supply an explicit start and authorization fingerprint. The
eventual operational loader must verify the real activation artifact before
constructing this capability; no such loader exists in B2. Only S strictly after
that start is prospective. An unset gate blocks reservations. An absent frozen
symbol-rule fingerprint also blocks reservations. Neither is filled in implicitly.

The committed equity reference is the draft's INITIAL_REALIZED_BALANCE of 1000
USDT, fingerprinted with the draft identity. B2 has no financial accounting or
equity update API. B3 must explicitly extend the lifecycle/accounting binding for
subsequent trades; no starting-equity substitution into an existing ledger exists.

## Boundary and information boundary

`AcceptanceStore.accept(SourceAdapter, S, clock=...)` is the persistence entry
point. It requires the exact read-only B1 adapter and invokes its normal source
authorization, checkpoint and observation/receipt verification. Raw dictionaries,
partial batches, duplicate boundaries in a source snapshot, changed identities,
and inconsistent data cannot enter the normal persistence path.

`BoundaryBatch.from_evidence` projects only three symbol/direction/ATR/hash values,
S, effective timing, availability timestamp and provenance hashes. It carries no
candles, opening price, context values, sizing results or performance. The pure
arbitrator has no future-price argument or execution dependency. Context can
change the bound source hash but cannot influence symbol selection.

E is always S+900000 ms. The deadline is E−60000 ms (S+14 minutes), inclusive.
The receipt must already be durably readable; its timestamp must be strictly
less than the acceptance timestamp. No boundary or receipt from the future is
accepted. The clock is an explicit integer-UTC-ms dependency, not a backdated
timestamp field. A future consumer must supply its trusted operational clock;
B2 supplies neither a running consumer nor an implicit wall-clock/activation.

## Dispositions and arbitration

The complete BTCUSDT / ETHUSDT / ADAUSDT batch is evaluated in that fixed order.
No candidate is retained for fallback. In order, each candidate receives:

| Condition | Disposition |
|---|---|
| Signal NONE | NONE (always audited) |
| Effective timing DELAYED_RECONSTRUCTED | MISSED_DELAYED_SIGNAL |
| No verified receipt or not ONLINE | BLOCKED_UNCONFIRMED_SOURCE |
| Acceptance after the fixed deadline | MISSED_ACCEPTANCE_DEADLINE |
| Prospective gate unset | BLOCKED_NOT_ACTIVATED |
| S at/before prospective start | OUTSIDE_PROSPECTIVE_INTERVAL |
| Frozen rules binding absent | BLOCKED_CONFIGURATION |
| ATR equals zero | REJECTED_INVALID_ATR |
| Global slot occupied/unresolved | BLOCKED_GLOBAL_SLOT |
| A higher-priority eligible candidate selected | BLOCKED_PRIORITY |
| First eligible candidate | SELECTED_PENDING_CONFIRMATION |

Negative/nonfinite ATR or malformed source values fail source validation before
arbitration. Zero ATR is valid evidence but not entry-eligible. A lower-priority
candidate may be selected when a higher candidate fails an acceptance-known gate;
it can never be selected due to a later entry price, sizing outcome or rejection.

The selected reservation freezes symbol/direction/ATR, signal hash, exact source
observation and receipt hashes, evidence identity, S, E, deadline, acceptance
timestamp, equity basis/reference, rules/configuration and implementation binding.

## Durability and deadline confirmation

The first `BEGIN IMMEDIATE` transaction commits the entire canonical batch,
all three dispositions, original source evidence, one immutable reservation
(if selected), and acquisition of B1's singleton global slot. Any failure rolls
back all of those writes. There is no partially committed three-symbol decision.
The physical B1 slot phase PENDING denotes a B2 pending-entry reservation.

A selected instruction is **not usable** until a post-commit UTC sample and
independent monotonic elapsed time corroborate durability by the inclusive
deadline. The monotonic interval starts immediately before the acceptance UTC
sample and ends immediately before the post-commit UTC sample, bracketing the
database commit, including any pause while or after sampling acceptance. Its
integer millisecond floor is a conservative corroboration threshold; acceptance
sampling latency can make this stricter, never extend the deadline. Confirmation requires
`durable_ms >= acceptance_ms + floor(elapsed_ns / 1_000_000)` as well as
`durable_ms <= deadline_ms`. No tolerance can extend the deadline. A clock
rollback masked by a long commit therefore cannot manufacture timely acceptance.
A second append-only confirmation transaction records the UTC sample, elapsed
nanoseconds, minimum corroborated timestamp and any clock-error code. Absolute
monotonic readings (which have process/system-specific origins) are not stored.

- PENDING_ENTRY: both clocks corroborate the sample, at/before the deadline.
- MISSED_ACCEPTANCE_DEADLINE: first commit completed too late; slot becomes BLOCKED.
- BLOCKED_CLOCK: UTC/monotonic regression, disagreement or invalid sample;
  slot becomes BLOCKED with an explicit auditable reason.
- UNCONFIRMED_ACCEPTANCE: no durable confirmation exists (e.g. crash between
  commits); the pending slot remains occupied and restart never upgrades it.

This conservative confirmation is part of acceptance safety, not a paper fill.
An instruction may be confirmed after E only if its preserved post-commit sample
already proves timely durability; later settlement is future B3 work. A missing
sample cannot be reconstructed from a later clock. Returned effective dispositions
resolve the selected candidate's status without rewriting original arbitration;
lower-priority BLOCKED_PRIORITY records remain permanent in every case.

B2 does not auto-clear a blocked/unconfirmed slot, release rejected instructions,
or implement B3 rejection processing. Recovery/lifecycle changes require a later
reviewed phase. No finalization path reopens the signal boundary for fallback.
The UTC anchor still must come from the future consumer's trusted local clock;
monotonic corroboration is not an external UTC oracle. No network is needed.

## Storage, restart and identity

Only `artifacts/forward-paper-execution/b2-*/paper.sqlite` is allowed, separately
from B1 draft stores and LIVE-0A. No real directory/run is created by this phase.
B1's schema is reused with additional append-only `batches`, `reservations` and
`confirmations` tables. All financial values remain canonical Decimal strings in
TEXT; timestamps are integer UTC milliseconds. There are no financial REAL
columns. An OS ownership lock and SQLite write transaction serialize writers.

A unique boundary key identifies each opportunity. Retrying identical evidence
returns the original decision/confirmation without sampling a new acceptance
clock, reserving again or re-arbitrating. Conflicting observation/receipt identity
fails closed. Even a newly available receipt cannot silently reopen a previously
audited unconfirmed boundary. New boundaries and acceptance times must progress
chronologically. B2 supports only the first outstanding reservation; it has no
settlement/release API. The singleton slot is global across all symbols.

On reopen, identity, database integrity, foreign keys, canonical source proofs,
deterministically recomputed decisions, reservation linkage, confirmation timing
and slot ownership are checked. Recomputing the pure acceptance decision for
verification does not evaluate a strategy or financial result. Unknown or
inconsistent state fails closed.

## Offline verification

Tests cover complete batches, signals/NONE, timing/deadline edges, source
availability, prospective/configuration gates, priority, invalid ATR, one slot,
atomic rollback, confirmation failure/clock regression, durable restart, exact
idempotence, conflicting replay, missing receipts, source tampering, permanent
BTC/ETH selection after hypothetical rejection, no future-price input, immutable
audit, separate writers, and normal read-only source-chain integration.

The B1 import allowlist test now targets its four frozen modules explicitly;
B2 has its own strict allowlist. No B1 behavior or identity changed. Tests never
open the real LIVE-0A evidence database or change its scheduler. No dependency is
added. Activation, B3 and all operational consumer/scheduler work remain absent.
