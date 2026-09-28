# LIVE-0B B4: durable paper lifecycle

All records are **FORWARD_PAPER / NON_EXECUTING**. This phase adds explicit local
ledger operations, not a running consumer, scheduler, reporting CLI or activation
writer. No `live-0b-001` run or T0 is created. The real frozen rule snapshot is
still unresolved. Tests use temporary synthetic B4 directories and synthetic
prospective gates only. The default unset B2 gate cannot accept entries.

## Integration and historical identity

`paper.journal` is a deterministic reducer. `paper.ledger.PaperLedger` owns one
separate SQLite database under `artifacts/forward-paper-execution/b4-*`, protected
by the existing process ownership lock. Other namespaces, aliased database files
and activated run names are rejected.

B2's original store deliberately supports one lifetime reservation and initial
equity only. B4 therefore owns its lifecycle journal and calls the same B2
`decide` and post-commit UTC/monotonic confirmation functions. It does not mutate
or import a previous B2 database. It does not infer acceptance from source candles.

Two narrow integration changes are explicit:

- `decide(..., equity_basis=...)` accepts immutable canonical bytes containing
  the current realized balance, draft identity and previous accounting event.
  Its default remains the original B2 initial-capital basis. Selection, timing,
  priority, eligibility and no-fallback logic are unchanged.
- `ConfirmedEntry` accepts that same explicit basis for validating its B2
  record. B3 settlement, sizing, protections and accounting formulas are
  unchanged. The B4 reducer verifies the supplied basis against its equity chain.

These source changes intentionally produce **new current B2/B3 implementation
identities**. They are not represented as unchanged historical fingerprints.
The B4 identity binds current implementations and these historical ancestors at
commit `9f33ecfdbf604bb5425995f2235f52f6263ebb14`:

- B1: `f3f5e761bcbda57ff75dfb2d514bfc17cd8a54ff57dc79e7e36e5bd4e8aab353`
- B2: `3a51e20e00f3f6faaf3b7cb3f2e575996accc85b978657bbdc43979a4712f8b1`
- B3: `75502f8a4509239a6fb0d34bd4a54226cac561a5868e90007d34f62d680e9170`

The ancestor hashes remain reproducible from those committed sources. No old
artifact, identity record or ledger is rewritten. There is no automatic upgrade
of an old B2/B3 ledger. B1, research, LIVE-0A and Semantic Contract V2 sources and
identities remain unchanged. The B4 implementation identity binds its two new
modules plus the current B3 dependency/runtime chain and historical ancestry.

## Authoritative journal and projections

The append-only `journal` has contiguous sequence numbers, unique deterministic
request IDs, parent hashes, canonical input/result payloads and event hashes.
Event kinds are ACCEPT, CONFIRM, ENTRY and BAR. Missing-candle attempts use
separate WAIT request keys, preserve exposure and are idempotent; subsequent
available evidence can produce the actual financial transition.

Each ACCEPT stores the complete LIVE-0A observation/receipt/source identity,
unchanged B2 dispositions/reservation and the committed equity reference. A
CONFIRM stores the original durable-time/monotonic proof. Interrupted acceptance
without confirmation permanently remains non-executable; retry does not resample
its clock or invent confirmation. Failed confirmation keeps the slot BLOCKED.

Materialized tables are checked against deterministic journal replay:

| Table | Purpose and constraints |
|---|---|
| identity | Immutable exact B4 binding, rules, ancestors and implementation |
| reservations | Unique owner/boundary; immutable reservation payload; lifecycle state |
| positions | One position per reservation, including financial fields and cursor |
| fills | One ENTRY and one EXIT per owner, unique deterministic digests |
| candles | Immutable complete source-candle evidence by digest |
| processed | Unique owner/15m opening linked to exact source-candle evidence |
| trades | One immutable CLOSED trade per position |
| equity | One immutable accounting event per CLOSED trade; ordered sequence and financial availability time |
| global_slot | Exactly one materialized pending/open/blocked owner or EMPTY |
| account | Current realized equity, accounting head, financial/source-state availability watermarks |

An additional partial unique index permits at most **one active reservation
total**, including unconfirmed, pending, blocked and open states across all
symbols. Immutable tables reject UPDATE/DELETE through triggers. Foreign keys,
unique keys, state checks and full reconciliation supplement transaction safety.
Financial columns are TEXT and JSON Decimal strings, never SQLite REAL. Times
are canonical integer UTC milliseconds. No wall-clock timestamp or local path
enters a computational identity; actual acceptance evidence is deliberately
part of the accepted instruction identity.

## Transactions and realized equity

Writes use BEGIN IMMEDIATE, foreign keys, FULL synchronous commits and the
exclusive local ownership lock. The journal event and all corresponding
projections commit together:

1. ACCEPT atomically records observation/receipt, B2 decision/reservation,
   committed equity and pending slot. The post-commit confirmation remains a
   separate event, preserving B2 crash/deadline semantics.
2. ENTRY atomically records exact candle provenance, entry fill/fee, quantity,
   frozen ATR protections, requested/actual risk/utilization, OPEN position and
   slot transition. A definitive rejection records its result and releases the
   slot atomically. Lower-priority candidates remain permanently blocked.
3. A no-exit BAR atomically records its candle and next cursor. The entry candle
   is always processed first. Missing data cannot move the cursor or force an
   END_OF_DATA closure.
4. A closing BAR atomically records exit evidence/fill/fee/reason/phase, CLOSED
   trade, exactly one equity event, current balance and slot release.

Initial equity is 1,000 USDT. Entry fees are recorded but do not change realized
account equity until closure. Each equity event binds the previous event (or
genesis identity), trade, equity_before, net_pnl and equity_after. Under the
existing fixed-precision Decimal context:

`equity_after = equity_before + gross_pnl - entry_fee - exit_fee`.

Slippage is already embedded in fill prices and is not deducted again. Subsequent
reservations freeze this current closed-trade balance, not the starting balance.
Nonpositive realized equity cannot authorize another reservation. Funding and
liquidation remain excluded.

## Durable temporal consistency

The pre-correction audit found that a fresh UTC/monotonic interval could be
locally valid after a clock regression between operations. An acceptance at
04:04 could therefore consume equity from the already-processed 04:00–04:15
candle. Comparing only against the prior acceptance time was insufficient.

The repaired reducer maintains two deterministic, durable watermarks:

- `financial_available_ms`: the earliest supported availability of current
  realized equity. Each closure takes the maximum of the preceding equity
  watermark, its source observation/receipt, acceptance confirmation, entry
  candle receipt, all processed protection-candle receipts and exit receipt.
- `state_available_ms`: the maximum of incorporated source receipt times and
  claimed acceptance/confirmation times, starting from the same initial
  availability binding. This also covers open-position evidence and definitive
  rejections before another operation consumes their resulting state.

ACCEPT requires its claimed time to be **greater than or equal to both known
watermarks**. Equality is allowed. Existing B2 receipt-availability and inclusive
deadline rules still apply independently. An executable CONFIRM must also meet
the durable state floor. Invalid local B2 clock confirmations retain their
original non-executable semantics.

Closed-trade events bind the prior financial watermark, maximum supporting
evidence availability, resulting financial watermark, exact exit evidence ID
and exit receipt time. The equity table stores the resulting availability as
INTEGER milliseconds. The account projection stores both watermarks. Replay
derives these again from preserved source/acceptance evidence and checks every
projection; it does not trust a mutable watermark column on its own.

A temporal failure rolls back the attempted operation, creates no pending
reservation and writes a durable `TEMPORAL_EVIDENCE_REGRESSION` BLOCKED latch
with the original claimed time and both floors. No timestamp adjustment,
fallback candidate, reordering or repair is allowed. The same check runs during
restart/reconciliation, including on correctly hash-linked but causally invalid
journal events. Previously recorded acceptance retries return their original
result/equity basis and never perform a new acceptance using later equity.

Initial 1,000 USDT availability is bound to the existing explicit prospective
activation-gate capability. With its boundary unset, initial financial
availability is `NULL` and no executable reservation is possible. A future
activation must bind it to the eventual activation boundary. This phase does
not choose a boundary, create an activated run or migrate a real database.
Only synthetic tests supply a gate. Availability is never inferred from startup
wall-clock time. Late settlement may process old candles; their market timestamps
are not falsely treated as claims about when the settlement operation occurred.

No B3 fill, protection, fee, slippage, sizing or P&L formula changes are involved.

## Reconciliation, replay and source isolation

Reopen and every transition validate SQLite integrity, foreign keys, identity,
append-only triggers, journal hashes/request identities and all projections.
Replay uses only preserved acceptance evidence and B3 arithmetic; it has no
clock sampling, source download or strategy evaluation. Expected fills, trades,
cursor histories, slot ownership and equity chain must agree exactly. No orphan
or contradictory materialized state is automatically repaired.

Before new acceptance, source provenance is rechecked through exact approved
`SourceAdapter` and `CandleSource` instances. These only read LIVE-0A. Previously
stored observation/receipt and candle evidence must still match. A newly added
receipt for a previously unconfirmed observation does not change the already
recorded B2 disposition or create an acceptance. Market operations also perform
source reconciliation before using their exact requested candle.

Temporary absence of the next required candle records an unresolved result and
retains pending/open exposure. Conflicting evidence, unauthorized source,
financial contradiction or integrity failure creates an exclusive, fsynced
`BLOCKED.json` audit latch in the **B4 directory**, independent of SQLite so that
database failure can still be recorded. The latch prevents subsequent operations
and is never automatically cleared. Existing financial evidence is not repaired.

Repeated committed request keys return the identical stored result; conflicting
inputs fail closed. A returned replay result describes that historical operation,
not necessarily the current slot state. There is no durable replay worker in
this phase: callers explicitly invoke `accept`, `settle_entry` or
`process_candle`. `reconcile` reconstructs/validates local state without creating
missing acceptances or replaying strategy signals.

## Crash validation and operational limits

Offline tests inject process-style failures before writes, after journal insert,
after projection updates and after commit. Entry, rejection/release, no-exit
cursor progression, closure/accounting and interrupted acceptance are covered.
For financial transitions, restart plus retry must converge to the uninterrupted
journal and state, with no duplicate fill, fee, trade, accounting event or
resurrected candidate. Acceptance timing proof has the distinct safe recovery
semantics below.

CONFIRM-specific tests now cover rollback after journal insertion, rollback
after materialization and interruption immediately after commit. A rolled-back
CONFIRM converges to stable **UNCONFIRMED_ACCEPTANCE**, retaining the single
slot; it is never recreated later. A committed CONFIRM survives restart as the
same PENDING_ENTRY. This intentional safety distinction preserves B2's rule
against inventing a missing durable timing proof.

The initial implementation favors exhaustive replay/source comparisons over
incremental caching. Long-running replay/read cost grows with journal history;
any future optimization must retain these checks. This is a local single-owner
database design, not a distributed ledger. Cryptographic hashes detect alteration
relative to the bound chain/checkpoints; they are not external signatures.

No scheduler, B5 reporting, activation command, real rules snapshot, market-data
fetching or credential/order capability is added. No real LIVE-0A data is needed
by the tests, and no operational LIVE-0A artifact or Windows task is modified.

## Pre-correction verification record

- Before the temporal audit: B4 41 tests passed; B1/B2/B3 105 passed; focused total 146.
- Full offline suite: 733 passed using the project Python 3.12.10 runtime.
- AST/compile, whitespace, import isolation and `git diff --check` passed.
- Historical B2/B3 fingerprints above were recomputed from the committed
  ancestor, without rewriting artifacts. B1, B2 policy, LIVE-0A and Semantic
  Contract V2 identities verified unchanged.

Integrated B2/B3 identities are unaffected by the temporal correction:

- B2 equity-input integration:
  `d481c53caeae3246958ad6baf6aa3e90ef7c813ad47285d18eb09aca642238c1`
- B3 equity-proof integration:
  `f3134c933fdbe4d0937a83af2d5844e9545a25ac75068b295226a6d7a007869a`
- Superseded pre-correction B4 implementation/runtime (not approved for use):
  `e8a631c4e34249c5cae98ee5fe50623ef2f9f4d376270572a87effcd38d6ac9d`
