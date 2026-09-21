# LIVE-0A.1a immutable operational patch authorization

FORWARD_PAPER / NON_EXECUTING. No real patch artifact is written by implementation
or tests. No scheduling, polling, reconstruction or strategy execution occurs.

`operations-v1.json` remains the immutable root. Its fingerprint, original
effective boundary and timing policy are never replaced. The optional
`operations-patch-v1.json` is one specifically supported encoding-patch lineage,
not a general migration registry. No second patch version or arbitrary ancestry
is accepted.

The sidecar binds the root fingerprint, its old implementation, the reviewed
encoding-only implementation, the final implementation including authorization
support, scope/reason, Semantic Contract V2, unchanged policy fingerprint, original
boundary, explicit patch effective UTC timestamp and complete pre-authorization
evidence checkpoint. Its fingerprint is SHA-256 over canonical JSON without its
own fingerprint field; financial values remain strings in inherited evidence.

Only the reviewed UTF-16 task serialization and authorization plumbing in
`forward/operations.py` may differ from the reviewed runtime. The scope check
reconstructs the encoding-only identity using the original operations module from
commit `70b08e260b046d2ae22983ce0194b27939be029b`; every other scoped source and
Python/pandas/NumPy version must match that reviewed identity. The final hash
additionally binds the entire current operations module. The historical candidate
and source verification gates remain in place.

Explicit Python APIs (no automatic import/startup actions):

- `plan_patch(directory, effective_ms=...)` reads and validates the root and current
  evidence; returns a deterministic proposal without writing anything.
- `bind_patch(directory, effective_ms=..., approved_implementation=...,
  approved_patch=...)` recomputes the proposal under the existing ownership lock.
  Any evidence change invalidates prior approval. It writes once with exclusive
  creation, flush and fsync; it never edits the database or root file.
- Normal `load_operations` verifies canonical sidecar bytes, exact ancestry,
  current implementation, scope and both root/patch evidence prefixes. It rejects
  missing required patches, corrupted files, unknown implementations and changed
  evidence. It returns the original root unchanged, so existing receipts continue
  using their original operational identity and tolerances.

The patch effective timestamp is a not-before gate for authorizing patched code;
it is **not a new signal timing-policy boundary**. Before that timestamp, normal
loading fails closed. Binding can occur beforehand, but execution cannot. No
ONLINE/DELAYED label is recalculated. A future poll may append evidence after the
checkpoint; prefix verification still protects existing rows.

Do not poll to test authorization: normal polling latches critical authorization
failures. Review proposals and verify loaders without calling poll. The real
sidecar requires separate exact fingerprint approval; none is activated here.

Hashes detect accidental alteration and enforce approved local identities; they
are not digital signatures against an administrator who can rewrite code and all
artifacts together. No source verification is disabled to preserve an old hash.
