# LIVE-0B B3: pure paper settlement and protections

Every record is **FORWARD_PAPER / NON_EXECUTING**. B3 has no network client,
exchange execution, consumer, scheduler, activated run or selected start time.
The actual activation symbol-rule snapshot remains unresolved. All test rules,
source databases, reservations and candles are synthetic.

## Boundary and provenance

`paper.market.RuleSnapshot` requires explicitly supplied normalized rules for
BTCUSDT, ETHUSDT and ADAUSDT in that order, a version and a source identity.
It fingerprints canonical Decimal rule values. Quantity limits must already
represent the applicable frozen market-order constraints; B3 neither downloads
exchange filters nor invents missing values. No activation snapshot is created.

`CandleSource.at(symbol, E)` performs the unchanged LIVE-0A production
manifest/root/patch authorization and source checks, then opens its database
through the existing read-only, query-only snapshot connection. It validates
checkpoints and BLOCKED state and returns only the exact requested closed
candle, its stored digest, receipt time and bound source identity. Missing data
returns `None`; corrupt or unauthorized source data raises without writing,
repairing, fetching or releasing exposure. No writable connection is exposed.

`ConfirmedEntry` validates the complete canonical B2 result against its binding,
source observation/receipt, irrevocable selected candidate and UTC/monotonic
timing confirmation. Only `PENDING_ENTRY` is accepted. Missing, late or
BLOCKED_CLOCK confirmation fails closed. The original selected symbol, frozen
ATR, committed equity and E are never replaced. Validation of B2's recorded
decision does not select a different candidate. No prices enter this check.

These typed records prove internal provenance consistency, not possession of a
durable reservation by an arbitrary caller. B4 must load them from the verified
B2 database, verify the occupied global slot, and atomically persist each
transition. B3 supplies no public activation or worker entry point.

## Entry and sizing

`settle()` consumes that confirmed instruction, an exact `CandleEvidence` and
the rules snapshot whose fingerprint B2 committed. Only the candle's **open**
affects entry finances. Eventual H/L/C/V affects source identity but cannot
affect entry selection, price, size or protections. Settlement after candle
completion represents the instruction committed before E, never a new decision.

For LONG, P = open * 1.0002; for SHORT, P = open * 0.9998. ATR remains frozen
from B2. LONG stop/target = P - 2 ATR / P + 4 ATR; SHORT = P + 2 ATR / P - 4 ATR.
Tick rounding is away from entry: LONG stop down/target up, SHORT stop up/target
down. Levels must be positive, strictly adverse/favorable and noncollapsed.

Requested risk is committed realized equity * 0.01. Risk quantity divides it
by the **rounded** stop distance. The existing sizing function caps quantity
by equity / (P / 3 + P * 0.0005) and the frozen maximum quantity, rounds down to
the quantity step and enforces minimum quantity/notional. Requested risk, actual
planned stop-distance risk and utilization are separate fields. Entry fee is
P * quantity * 0.0005. The B2 foundation currently binds its initial 1,000 USDT
basis; future realized-equity ledger integration is not introduced here.

For example, P=100.02, ATR=1, tick=.3 gives stop=97.8, distance=2.22. With a
.01 quantity step, quantity=4.50, planned risk=9.99 against requested risk=10.
Caps can lower utilization; they never force higher leverage to use the budget.
Planned stop-distance risk is **not a guaranteed maximum realized loss**:
fees, adverse slippage and gaps can increase losses.

Definitive constraints produce `REJECTED_SIZING` or `REJECTED_EXECUTION` with a
FREE target slot state. Missing/wrong source or rules produce `UNRESOLVED`.
Neither result offers a fallback symbol; all original BLOCKED_PRIORITY records
remain final. FREE is an output for a future atomic B4 transition, not a database
mutation performed by B3.

## Chronological protection and accounting

`protect()` requires the entry candle first, then exactly consecutive completed
15m candles. It reconciles the original financial position on each call. A
different revision of the entry candle is rejected. Repeated, skipped, wrong
symbol/source or absent candles retain unresolved exposure rather than close
or release it. Cursor and chained candle identity record processing progress.
B4 must bind these to its append-only transition history; an in-memory cursor
alone is not an independently authenticated durable replay ledger.

Entry-bar protections are active: that same candle may immediately close the
position. Processing reuses the historical pure `process_bar` and
`close_position` arithmetic: stop opening gap first, target opening gap second,
then exact-touch intrabar checks. Unknown dual-touch ordering resolves to
STOP_LOSS. Stop gaps use the worse open; target gaps get no favorable price
improvement. Known opening chronology takes precedence over later dual touches.

Each exit applies adverse slippage once and fee 0.0005 once. LONG gross P&L is
(exit - entry) * quantity; SHORT reverses the sign. Net P&L deducts entry and exit
fees exactly once. Slippage attribution is informational because it is already
included in fills. Exit phase is OPEN or INTRABAR with the candle opening
timestamp; there is no invented precise intrabar fill time.

No END_OF_DATA/worker-shutdown close exists. Missing future candles keep the
position unresolved. There is no trailing stop or ATR recomputation.

## Models, identities and reuse

Immutable `PaperPosition`, `PaperTrade` and `Transition` contain canonical
Decimal financial fields and deterministic provenance. B3 adds **no SQLite
schema or equity ledger**. Its target slot states are OPEN, UNRESOLVED and FREE;
the existing B2 global slot remains authoritative until B4 implements atomic
durable lifecycle changes. Repeating a pure call is deterministic and has no
side effects; applying its financial result exactly once is B4's responsibility.

The scoped B3 implementation fingerprint binds B2's runtime/source identity,
both B3 modules, and unchanged historical simulation/protection sources.
Rules, confirmed instructions, candles, positions, trades and transitions each
have deterministic canonical identities, without wall-clock or local paths.

Direct reuse from `backtest.simulation`: fixed precision Decimal context,
rounding, slippage, sizing, Fill, financial config, protection processing and
close arithmetic. The two-line ATR formula mirrors the frozen protection
policy, with golden-vector rounding tests. No source extraction/refactor was
needed; no historical code or identity changes. No research view or Decision
is fabricated. `simulate`, `simulate_portfolio`, research feature execution and
research-only protection construction are never called.

Funding, liquidation, order books, real fill guarantees and intrabar sequencing
are excluded. Fees/slippage remain research assumptions. This is not an
executable live trading system.

## Offline validation

Focused tests cover timing confirmation, immutable selection/no fallback,
source authorization/read-only access/tampering, exact E, entry H/L/C/V
independence, LONG/SHORT entry/exit, historical rounding vectors, independent
risk examples, margin/fee/leverage/filter caps, entry-bar stops/targets/dual
touches, gaps, chronological gaps/duplicates, unresolved exposure, deterministic
replay, Decimal isolation, canonical records and import/call restrictions.

Use the stable project runtime for focused and full offline unittest discovery.
No real LIVE-0A evidence is needed by these tests. B1/B2 and inherited LIVE-0A
and Semantic Contract V2 identities must remain unchanged. No B4 work is enabled.

Completed verification with the project Python 3.12.10 runtime: 35 B3 tests,
29 B1 tests and 41 B2 tests passed (105 focused); 692 full offline tests passed.
AST/compile, whitespace, import isolation and `git diff --check` passed.
B1 configuration/implementation, B2 policy/implementation, LIVE-0A implementation
and Semantic Contract V2 fingerprints verified unchanged.

B3 implementation/runtime fingerprint:
`75502f8a4509239a6fb0d34bd4a54226cac561a5868e90007d34f62d680e9170`.
