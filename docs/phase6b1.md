# Phase 6B1: specifications and research audit registry

This phase contains metadata models only. There is no command-line runner,
strategy implementation, aggregator, downloader, scorecard calculation or data
loader. Importing the package has no filesystem or network side effects.
No actual research registry, experiment, promotion or holdout authorization was
created during implementation. Tests use temporary journals and synthetic hashes.

## Approved policy

`research.spec.policy()` returns a fresh copy of research-policy-v1. The complete
policy is embedded in each immutable experiment specification and its fingerprint.

| Role | Half-open UTC evaluation interval |
| --- | --- |
| DEVELOPMENT | [2024-01-01, 2025-01-01) |
| LOCKED_PREVIOUSLY_OBSERVED_VALIDATION | [2025-01-01, 2026-01-01) |
| TRUE_HOLDOUT | [2026-01-01, unset endpoint) — inaccessible |

2025 was already observed through baseline-001 and Phase 6A. It is not genuinely
unseen validation data. No 2026 data is accessed by this package or its tests.

Every split starts flat at 1,000 USDT, with no equity or positions carried from
another split. Warmup is history-only and excluded from trade/scoring eligibility.
The only approved development concepts are EMA 9/21 with 15m, 1h and 4h signals.
The registry permits one computational experiment identity per timeframe, three
total. These registrations are not permission to execute anything in Phase 6B1.

Frozen settings include BTC/ETH/ADA priority and universe, both directions,
one position, 1% risk, 3x leverage, SL 1.5%, TP 3%, 15m execution, the existing
gap/dual-touch conventions, research-rules-v1 fingerprint, fee 0.0005 and adverse
slippage 0.0002 per fill. Costs and current static rules remain research
assumptions, not actual historical fees/rules. Funding/liquidation remain excluded.

Eligibility is stored, not calculated: positive development net expectancy,
net PF >1, activity in all four quarters, positive net P&L in at least three,
MTM drawdown strictly below 25%, and no integrity failure, insolvency or unexplained
reconciliation error. One hundred completed trades is an evidence target, not a
hard rejection threshold. Materially smaller samples must be labeled weaker
evidence; the quantitative meaning of materially smaller remains unspecified.

## Immutable identities

`ExperimentSpec` is a frozen dataclass with:

- signal timeframe and strategy version;
- complete approved policy (including family, parameters, split intervals,
  execution/risk/cost settings, evidence policy and rule fingerprint);
- sorted source-name/SHA-256 pairs;
- exactly three symbol/development-dataset fingerprint pairs;
- sorted software/version pairs.

Actual future source and split dataset fingerprints must be supplied before an
experiment can be registered. Placeholder identities are not created in this
phase. Validation/holdout dataset hashes belong to their later run records,
not to premature data inspection. A run fingerprint binds the experiment,
authorized split/warmup interval and dataset fingerprints.

SHA-256 covers canonical sorted-key ASCII JSON. Decimal values serialize as
normalized strings, negative zero becomes zero, and floats/nonfinite values are
rejected. No timestamps, random values, filesystem paths or result metrics enter
experiment identity. Record properties return fresh structures; modifying them
does not mutate their source dataclass.

`Hypothesis` records question, mechanism, comparison, parent experiment IDs and
whether validation informed the hypothesis. Parents must already be registered,
which prevents forward references/cycles. Hypothesis identity is distinct from
computational identity. A second hypothesis referencing the same experiment
creates a DUPLICATE event, not another trial. The frozen version makes source
changes a new computational identity, so a changed implementation in an occupied
timeframe slot requires future explicit budget-policy revision.

## Journal behavior

Construct `Registry(directory)` with a dedicated research directory; no live
runtime/persistence module is used. Do not place it in baseline-001. No default
production registry path or application startup integration is provided.

The journal is canonical JSONL. Each event contains a sequence number, previous
event hash, kind, payload and event hash. Every mutation first validates the
existing chain, then appends and flushes/fsyncs. Reads return detached records.
There is no update/delete API. Original bytes remain a prefix after appending.
An exclusive writer lock serializes concurrent writes. A preexisting/stale lock
or malformed/partial journal fails closed; stale locks and damaged tails require
manual recovery and are never silently removed or truncated.

Event kinds include HYPOTHESIS, EXPERIMENT, DUPLICATE, REJECTED_PROPOSAL, ACCESS,
ATTEMPT, OUTCOME, VALIDATION_PROMOTION, HOLDOUT_AUTHORIZATION, VIEWED, DISCLOSED
and PRIOR_EXPOSURE. Rejected proposals can retain their specification and reason
without pretending that a financial experiment completed. Future orchestrators
must explicitly record rejections and disclosures; this metadata layer cannot
detect a human viewing a file outside its API.

An ATTEMPT is metadata only. Its first outcome is permanent. A FAILED attempt may
be followed by another numbered attempt with the same run identity. An active
or completed attempt cannot be silently retried. Split inputs cannot be replaced
under the same experiment. Repeated registrations and attempts remain auditable
but are not independent evidence. Unknown outcomes and invalid transitions fail.

## Access and promotion

`SplitRequest` accepts the explicit role enum and exact evaluation interval, using
aligned integer milliseconds. `record_access` checks authorization before
recording READ or ACQUISITION metadata. Future loaders must call this *before*
opening/downloading data. Development requires no promotion; validation requires
a frozen promotion for that experiment. Unknown experiments and altered splits
fail closed. Each request requires exactly 99 completed signal candles of
warmup, and endpoints aligned to its signal timeframe. This does not aggregate
or prove candle completeness; later data code must validate the actual candles.

Promotion requires completed development evidence plus explicit operator and
rationale. It records a reference to the evidence event, not computed scorecard
eligibility. Holdout requires completed validation evidence plus a separate
explicit operator authorization. Completion is not an automatic PASS: the human
must assess promotion until a later scorecard implements policy evaluation.

The first holdout authorization freezes one aligned endpoint globally within
that journal. Subsequent candidates cannot choose a different endpoint; a given
candidate's authorization cannot be replaced. Unauthorized reads/acquisitions
and requests extending the endpoint fail. Authorization is necessarily earlier
in the journal than access. While endpoint is unset, all holdout access is denied.

These are application-level safeguards, not operating-system isolation or
authentication. Operator strings attest to a decision; they do not prove identity.
A person controlling local files can bypass an API or replace an entire journal.
Later execution/data access must integrate these guards, use a single governed
registry, and apply filesystem/process restrictions where appropriate. No
validation result selection or holdout access has occurred in Phase 6B1.

## Tests and deferred work

Offline tests cover canonical identities, immutable specs, duplicate slots,
retry semantics, ancestry, prior 2025 exposure, split boundaries, warmup exclusion,
promotion, holdout denial/frozen endpoints, access ordering, dataset substitution,
append-only persistence/reopen, corrupt tails, writer locking and disclosures.
No financial simulations are invoked by these tests.

Phase 6B2 aggregation, 6B3 execution/strategy integration and 6B4 scorecard
calculation remain unimplemented. No frozen baseline module or artifact is changed.
