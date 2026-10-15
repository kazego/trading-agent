"""Historical synthetic snapshot audit. Result data grants NO execution authority.

No PaperLedger constructor, consumer, source loader or original database is used.
Financial replay and projection comparison remain the existing B4 implementation.
"""
import sqlite3
import time

from paper.journal import implementation
from paper.ledger import PaperLedger
from paper.model import parse
from research.incremental_ledger import compact, VERSION
from research.incremental_snapshot import verify_sealed, synthetic_path, CaptureRejected
from research.spec import encoded, fingerprint


def replay_snapshot(db, anchor, binding, rules):
    """Read-only full replay; binding/rules are historical data, not capabilities."""
    if db.execute('PRAGMA query_only').fetchone() != (1,) or db.in_transaction:
        raise CaptureRejected('Fresh query-only connection required')
    db.execute('BEGIN')
    try:
        rows=db.execute('SELECT digest,payload FROM identity').fetchall()
        if len(rows)!=1:raise CaptureRejected('Unique genesis required')
        genesis,raw=rows[0];identity=parse(raw.encode())
        if (fingerprint(identity)!=genesis or identity['binding']!=binding.record
                or identity['rules']!=rules.record or anchor['reducer']!=implementation()
                or anchor['version']!=VERSION):
            raise CaptureRejected('Incompatible historical identities')
        verifier=object.__new__(PaperLedger)  # Read-only verifier, never initializing a ledger.
        verifier._db=db;verifier.identity=identity;verifier.identity_id=genesis
        verifier.binding=binding;verifier.rules=rules;verifier._runtime_identity=implementation()
        state=verifier._verify()
        count=db.execute('SELECT count(*) FROM journal').fetchone()[0]
        schema=fingerprint(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall())
        expected=dict(version=VERSION,reducer=implementation(),authorization=anchor['authorization'],
                      schema=schema,policy=anchor['policy'],sequence=count,
                      financial_count=len(state.accounting),state=compact(state))
        stored=db.execute('SELECT payload FROM incremental_state').fetchall()
        if expected!=anchor or stored!=[(encoded(anchor).decode(),)]:
            raise CaptureRejected('Full replay/compact state disagreement')
        projections=state.rows()
        return dict(sequence=count,head=state.journal_head,genesis=genesis,
                    state_hash=fingerprint(compact(state)),equity=str(state.equity),
                    financial_rows={k:projections[k] for k in ('account','global_slot','positions','fills','trades','equity')},
                    projections_hash=fingerprint(projections))
    finally:db.rollback()


def audit_sealed(directory, anchor, binding, rules, *, on_replay=lambda:None):
    """Open only the named sealed copy, return an unprivileged result to parent.

    Input anchor must come from independent continuity verification. No audit
    freshness renewal/publication is performed. This is not an OS access sandbox.
    """
    started=time.perf_counter_ns()
    manifest=verify_sealed(directory,anchor)
    path=synthetic_path(directory/'snapshot.sqlite')
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        replay_start=time.perf_counter_ns()
        on_replay()
        result=replay_snapshot(db,anchor,binding,rules)
        replay_ns=time.perf_counter_ns()-replay_start
    finally:db.close()
    if verify_sealed(directory,anchor)!=manifest:raise CaptureRejected('Snapshot changed during audit')
    result.update(version='synthetic-snapshot-audit-result-v1',authority='NONE',status='PASS',
                  snapshot_id=manifest['snapshot_id'],manifest_hash=fingerprint(manifest),
                  file_sha256=manifest['file_sha256'],captured_at_ns=manifest['captured_at_ns'],
                  reducer=anchor['reducer'],replay_ns=replay_ns,total_ns=time.perf_counter_ns()-started)
    encoded(result)  # Exact Decimal strings/integers only.
    return result
