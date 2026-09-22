"""B2 atomic boundary journal and single pending slot. No settlement capability."""
from pathlib import Path
import sqlite3
import time

from paper.model import SourceObservation, parse
from paper.source import SourceAdapter
from paper.store import SCHEMA
from paper.acceptance import AcceptanceBinding, BoundaryBatch, decide, timestamp, implementation
from research.spec import encoded, fingerprint
from runtime import DatabaseLock


EXTRA_SCHEMA = '''
CREATE TABLE batches (
    boundary_ms INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
    evidence_id TEXT NOT NULL REFERENCES evidence(digest), payload TEXT NOT NULL);
CREATE TABLE reservations (
    digest TEXT PRIMARY KEY, boundary_ms INTEGER NOT NULL UNIQUE REFERENCES batches(boundary_ms),
    symbol TEXT NOT NULL CHECK(symbol IN ('BTCUSDT','ETHUSDT','ADAUSDT')), payload TEXT NOT NULL);
CREATE TABLE confirmations (
    reservation_id TEXT PRIMARY KEY REFERENCES reservations(digest),
    digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
'''


class AcceptanceStore:
    def __init__(self, directory, binding):
        if type(binding) is not AcceptanceBinding:
            raise ValueError('Explicit B2 binding required')
        directory = Path(directory).absolute()
        if (directory.resolve() != directory or directory.parent.name != 'forward-paper-execution'
                or directory.parent.parent.name != 'artifacts' or not directory.name.startswith('b2-')):
            raise ValueError('Separate artifacts/forward-paper-execution/b2-* namespace required')
        path = directory/'paper.sqlite'
        if path.is_symlink() or path.exists() and path.stat().st_nlink != 1:
            raise ValueError('Aliased database forbidden')
        directory.mkdir(parents=True, exist_ok=True)
        self._binding = binding
        self._lock = DatabaseLock(path)
        self._lock.acquire()
        try:
            self._db = sqlite3.connect(path)
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            if not self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                self._db.executescript('BEGIN IMMEDIATE;\n'+SCHEMA+EXTRA_SCHEMA)
                for table in ('identity', 'source_observations', 'source_receipts', 'evidence',
                              'batches', 'reservations', 'confirmations'):
                    for operation in ('UPDATE', 'DELETE'):
                        self._db.execute(f'''CREATE TRIGGER no_{operation}_{table} BEFORE {operation} ON {table}
                            BEGIN SELECT RAISE(ABORT,'Append only'); END''')
                self._db.execute('INSERT INTO identity VALUES(1,?,?)', (binding.id, encoded(binding.record).decode()))
                self._db.commit()
            self.verify()
        except Exception:
            self.close()
            raise

    def close(self):
        if hasattr(self, '_db'):
            self._db.close()
        self._lock.release()

    def _evidence(self, evidence_id):
        row = self._db.execute('SELECT payload FROM evidence WHERE digest=?', (evidence_id,)).fetchone()
        if row is None:
            raise ValueError('Missing boundary evidence')
        record = parse(row[0].encode())
        item = SourceObservation(self._binding.draft.source, encoded(record['observation']),
                                 encoded(record['receipt']) if record['receipt'] else None)
        if item.id != evidence_id or item.record != record:
            raise ValueError('Boundary evidence identity mismatch')
        return item

    def verify(self):
        if self._binding.implementation_id != implementation():
            raise ValueError('Current B2 implementation no longer matches binding')
        if (self._db.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or self._db.execute('PRAGMA foreign_key_check').fetchall()):
            raise ValueError('B2 database integrity failure')
        if self._db.execute('SELECT * FROM identity').fetchall() != [
                (1, self._binding.id, encoded(self._binding.record).decode())]:
            raise ValueError('B2 binding changed')
        expected_reservations = {}
        for boundary, decision_id, evidence_id, raw in self._db.execute('SELECT * FROM batches ORDER BY boundary_ms'):
            decision = parse(raw.encode())
            evidence = self._evidence(evidence_id)
            expected = decide(self._binding, BoundaryBatch.from_evidence(evidence),
                              accepted_ms=decision['acceptance_ms'], slot=decision['slot_at_acceptance'])
            if expected != decision or boundary != evidence.boundary_ms or expected['decision_id'] != decision_id:
                raise ValueError('Boundary decision changed')
            o = parse(evidence.observation)
            if self._db.execute('SELECT source_id,boundary_ms,payload FROM source_observations WHERE digest=?',
                                (fingerprint(o),)).fetchone() != (
                                    evidence.source.id, boundary, evidence.observation.decode()):
                raise ValueError('Source observation provenance changed')
            if evidence.receipt and self._db.execute(
                    'SELECT digest,payload FROM source_receipts WHERE observation_digest=?',
                    (fingerprint(o),)).fetchone() != (fingerprint(parse(evidence.receipt)), evidence.receipt.decode()):
                raise ValueError('Source receipt provenance changed')
            if decision['reservation']:
                r = decision['reservation']
                expected_reservations[r['reservation_id']] = (r['reservation_id'], boundary, r['symbol'], encoded(r).decode())
        rows = self._db.execute('SELECT * FROM reservations').fetchall()
        if set(rows) != set(expected_reservations.values()) or len(rows) > 1:
            raise ValueError('Unbound or multiple B2 reservations')
        # No lifecycle/release support in B2: once occupied, never silently clear.
        slots = self._db.execute('SELECT * FROM global_slot').fetchall()
        if len(slots) != 1 or slots[0][0] != 1:
            raise ValueError('Global slot missing')
        _, phase, owner, symbol = slots[0]
        if rows:
            if phase not in ('PENDING', 'OPEN', 'BLOCKED') or (owner, symbol) != (rows[0][0], rows[0][2]):
                raise ValueError('Global reservation ownership lost')
        elif phase not in ('EMPTY', 'BLOCKED') or owner is not None or symbol is not None:
            raise ValueError('Unknown exposure owner')
        for owner, receipt_id, raw in self._db.execute('SELECT * FROM confirmations'):
            receipt = parse(raw.encode())
            r = parse(expected_reservations[owner][3].encode())
            expected = self._confirmation(r, receipt['durable_ms'], receipt['elapsed_ns'])
            if receipt != expected or receipt_id != fingerprint(receipt):
                raise ValueError('Acceptance confirmation changed')
            if receipt['status'] != 'PENDING_ENTRY' and phase != 'BLOCKED':
                raise ValueError('Failed confirmation must block slot')
        counts = {name: self._db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                  for name in ('batches', 'evidence', 'source_observations', 'source_receipts')}
        if counts['batches'] != counts['evidence'] or counts['batches'] != counts['source_observations']:
            raise ValueError('Orphan boundary provenance')
        expected_receipts = sum(self._evidence(row[0]).receipt is not None
                                for row in self._db.execute('SELECT evidence_id FROM batches'))
        if counts['source_receipts'] != expected_receipts:
            raise ValueError('Orphan receipt provenance')

    @staticmethod
    def _confirmation(reservation, durable_ms, elapsed_ns):
        # Keep invalid samples canonical and auditable, never silently authorize.
        error = None
        try:
            timestamp(durable_ms)
        except (TypeError, ValueError):
            durable_ms = None
            error = 'INVALID_UTC_SAMPLE'
        if type(elapsed_ns) is not int:
            elapsed_ns = None
            error = 'INVALID_MONOTONIC_SAMPLE'
        elif elapsed_ns < 0:
            error = 'MONOTONIC_REGRESSION'
        minimum_ms = (reservation['acceptance_ms'] + elapsed_ns // 1000000
                      if elapsed_ns is not None and elapsed_ns >= 0 else None)
        if error is None:
            if durable_ms < reservation['acceptance_ms']:
                error = 'UTC_REGRESSION'
            elif durable_ms < minimum_ms:
                error = 'UTC_MONOTONIC_DISAGREEMENT'
        status = ('BLOCKED_CLOCK' if error else
                  'MISSED_ACCEPTANCE_DEADLINE' if durable_ms > reservation['deadline_ms'] else 'PENDING_ENTRY')
        return dict(label=reservation['label'], version='live-0b-acceptance-confirmation-v2',
                    reservation_id=reservation['reservation_id'], durable_ms=durable_ms,
                    elapsed_ns=elapsed_ns, minimum_durable_ms=minimum_ms, clock_error=error, status=status)

    def _result(self, decision):
        # Fresh copies; original dispositions and selected identity remain immutable.
        result = parse(encoded(decision))
        reservation = result['reservation']
        result['confirmation'] = None
        result['reservation_state'] = None
        result['effective_dispositions'] = parse(encoded(result['dispositions']))
        if reservation:
            row = self._db.execute('SELECT payload FROM confirmations WHERE reservation_id=?',
                                   (reservation['reservation_id'],)).fetchone()
            confirmation = parse(row[0].encode()) if row else None
            state = confirmation['status'] if confirmation else 'UNCONFIRMED_ACCEPTANCE'
            if state == 'PENDING_ENTRY' and self._db.execute(
                    'SELECT phase FROM global_slot WHERE singleton=1').fetchone()[0] == 'BLOCKED':
                state = 'BLOCKED_GLOBAL_SLOT'
            result.update(confirmation=confirmation, reservation_state=state)
            for d in result['effective_dispositions']:
                if d['symbol'] == reservation['symbol']:
                    d['reason'] = state
        return result

    def accept(self, adapter, boundary_ms, *, clock):
        """Source verified first; no raw-input production entry point or retry clock reuse."""
        if type(adapter) is not SourceAdapter:
            raise ValueError('Read-only authorized SourceAdapter required')
        timestamp(boundary_ms)
        if boundary_ms % 14400000:
            raise ValueError('4h signal boundary required')
        matches = [item for item in adapter.snapshot() if item.boundary_ms == boundary_ms]
        if len(matches) != 1 or matches[0].source != self._binding.draft.source:
            raise ValueError('Exactly one authorized complete source batch required')
        evidence = matches[0]
        batch = BoundaryBatch.from_evidence(evidence)
        self._db.execute('BEGIN IMMEDIATE')
        try:
            self.verify()
            existing = self._db.execute('SELECT evidence_id,payload FROM batches WHERE boundary_ms=?',
                                        (boundary_ms,)).fetchone()
            if existing:
                if existing[0] != evidence.id:
                    raise ValueError('Conflicting boundary replay')
                result = self._result(parse(existing[1].encode()))
                self._db.rollback()
                return result
            latest = self._db.execute('SELECT max(boundary_ms),max(json_extract(payload,\'$.acceptance_ms\')) FROM batches').fetchone()
            monotonic_start = time.monotonic_ns()
            if type(monotonic_start) is not int or monotonic_start < 0:
                raise ValueError('Invalid initial monotonic sample')
            accepted_ms = timestamp(clock())
            if latest[0] is not None and (boundary_ms <= latest[0] or accepted_ms < latest[1]):
                raise ValueError('Nonchronological acceptance')
            slot = self._db.execute('SELECT phase FROM global_slot WHERE singleton=1').fetchone()[0]
            decision = decide(self._binding, batch, accepted_ms=accepted_ms, slot=slot)
            self._db.execute('INSERT INTO source_observations VALUES(?,?,?,?)',
                             (batch.observation_id, evidence.source.id, boundary_ms, evidence.observation.decode()))
            if evidence.receipt:
                self._db.execute('INSERT INTO source_receipts VALUES(?,?,?)',
                                 (batch.observation_id, batch.receipt_id, evidence.receipt.decode()))
            self._db.execute('INSERT INTO evidence VALUES(?,?)', (evidence.id, encoded(evidence.record).decode()))
            self._db.execute('INSERT INTO batches VALUES(?,?,?,?)',
                             (boundary_ms, decision['decision_id'], evidence.id, encoded(decision).decode()))
            reservation = decision['reservation']
            if reservation:
                self._db.execute('INSERT INTO reservations VALUES(?,?,?,?)',
                                 (reservation['reservation_id'], boundary_ms, reservation['symbol'], encoded(reservation).decode()))
                changed = self._db.execute("UPDATE global_slot SET phase='PENDING',owner_id=?,symbol=? "
                                           "WHERE singleton=1 AND phase='EMPTY'",
                                           (reservation['reservation_id'], reservation['symbol'])).rowcount
                if changed != 1:
                    raise ValueError('Global slot not free')
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        if reservation:
            # A crash here leaves UNCONFIRMED_ACCEPTANCE. Restart never upgrades it.
            # Start before acceptance sampling, covering a pause in/between that
            # sample and commit. Sampling latency can only make this stricter.
            monotonic_end = time.monotonic_ns()
            elapsed_ns = monotonic_end - monotonic_start if type(monotonic_end) is int else None
            confirmation = self._confirmation(reservation, clock(), elapsed_ns)
            with self._db:
                self._db.execute('INSERT INTO confirmations VALUES(?,?,?)',
                                 (reservation['reservation_id'], fingerprint(confirmation), encoded(confirmation).decode()))
                if confirmation['status'] != 'PENDING_ENTRY':
                    self._db.execute("UPDATE global_slot SET phase='BLOCKED' WHERE singleton=1")
        return self._result(decision)
