"""Isolated B1 evidence store. No entry reservation or execution API."""
from pathlib import Path
import sqlite3

from paper.model import DraftManifest, SourceObservation, parse
from research.spec import encoded, fingerprint
from runtime import DatabaseLock


SCHEMA = '''
CREATE TABLE identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL,
    payload TEXT NOT NULL);
CREATE TABLE source_observations (
    digest TEXT PRIMARY KEY, source_id TEXT NOT NULL, boundary_ms INTEGER NOT NULL,
    payload TEXT NOT NULL, UNIQUE(source_id,boundary_ms));
CREATE TABLE source_receipts (
    observation_digest TEXT PRIMARY KEY REFERENCES source_observations(digest),
    digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE TABLE evidence (
    digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE global_slot (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    phase TEXT NOT NULL CHECK(phase IN ('EMPTY','PENDING','OPEN','BLOCKED')),
    owner_id TEXT,
    symbol TEXT CHECK(symbol IN ('BTCUSDT','ETHUSDT','ADAUSDT')),
    CHECK((phase='EMPTY' AND owner_id IS NULL AND symbol IS NULL) OR
          (phase IN ('PENDING','OPEN') AND owner_id IS NOT NULL AND symbol IS NOT NULL) OR
          phase='BLOCKED'));
INSERT INTO global_slot VALUES(1,'EMPTY',NULL,NULL);
CREATE TRIGGER keep_slot BEFORE DELETE ON global_slot
BEGIN SELECT RAISE(ABORT,'Global slot cannot be removed'); END;
'''


class PaperStore:
    def __init__(self, directory, manifest):
        if type(manifest) is not DraftManifest:
            raise ValueError('B1 draft manifest required')
        directory = Path(directory).absolute()
        if (directory.resolve() != directory or directory.parent.name != 'forward-paper-execution'
                or directory.parent.parent.name != 'artifacts' or not directory.name.startswith('b1-')):
            raise ValueError('Separate artifacts/forward-paper-execution/b1-* namespace required')
        path = directory / 'paper.sqlite'
        if path.exists() and (path.is_symlink() or path.stat().st_nlink != 1):
            raise ValueError('Aliased database forbidden')
        directory.mkdir(parents=True, exist_ok=True)
        self._manifest = manifest
        self._lock = DatabaseLock(path)
        self._lock.acquire()
        try:
            self._db = sqlite3.connect(path)
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables:
                self._db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
                for table in ('identity', 'source_observations', 'source_receipts', 'evidence'):
                    for op in ('UPDATE', 'DELETE'):
                        self._db.execute(f'''CREATE TRIGGER no_{op}_{table} BEFORE {op} ON {table}
                            BEGIN SELECT RAISE(ABORT,'Append only'); END''')
                self._db.execute('INSERT INTO identity VALUES(1,?,?)',
                                 (manifest.id, encoded(manifest.record).decode()))
                self._db.commit()
            self.verify()
        except Exception:
            self.close()
            raise

    def close(self):
        if hasattr(self, '_db'):
            self._db.close()
        self._lock.release()

    def verify(self):
        if self._db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Paper database integrity failure')
        if self._db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Paper provenance foreign key failure')
        if self._db.execute('SELECT * FROM identity').fetchall() != [
                (1, self._manifest.id, encoded(self._manifest.record).decode())]:
            raise ValueError('Paper database manifest mismatch')
        # B1 has no transition API; a nonempty slot is not a valid B1 draft.
        if self._db.execute('SELECT * FROM global_slot').fetchall() != [(1, 'EMPTY', None, None)]:
            raise ValueError('B1 cannot contain exposure')
        for obs_digest, source_id, boundary, raw in self._db.execute('SELECT * FROM source_observations'):
            row = self._db.execute('SELECT digest,payload FROM source_receipts WHERE observation_digest=?',
                                   (obs_digest,)).fetchone()
            item = SourceObservation(self._manifest.source, raw.encode(), row[1].encode() if row else None)
            if (source_id != self._manifest.source.id or boundary != item.boundary_ms
                    or fingerprint(parse(item.observation)) != obs_digest
                    or row and fingerprint(parse(item.receipt)) != row[0]):
                raise ValueError('Stored source provenance mismatch')
        for item_id, raw in self._db.execute('SELECT * FROM evidence'):
            record = parse(raw.encode())
            item = SourceObservation(self._manifest.source, encoded(record['observation']),
                                     encoded(record['receipt']) if record['receipt'] else None)
            if record != item.record or item_id != item.id:
                raise ValueError('Stored evidence identity mismatch')
            obs_digest = fingerprint(parse(item.observation))
            if self._db.execute('SELECT payload FROM source_observations WHERE digest=?',
                                (obs_digest,)).fetchone() != (item.observation.decode(),):
                raise ValueError('Orphan evidence')
            if item.receipt and self._db.execute(
                    'SELECT payload FROM source_receipts WHERE observation_digest=?',
                    (obs_digest,)).fetchone() != (item.receipt.decode(),):
                raise ValueError('Orphan evidence receipt')

    def record_source(self, item):
        """Atomic idempotent provenance copy; later receipts append, never relabel."""
        if type(item) is not SourceObservation or item.source != self._manifest.source:
            raise ValueError('Wrong source identity')
        obs_digest = fingerprint(parse(item.observation))
        with self._db:
            old = self._db.execute('SELECT digest,payload FROM source_observations '
                                   'WHERE source_id=? AND boundary_ms=?',
                                   (item.source.id, item.boundary_ms)).fetchone()
            if old and old != (obs_digest, item.observation.decode()):
                raise ValueError('Conflicting source observation')
            self._db.execute('INSERT OR IGNORE INTO source_observations VALUES(?,?,?,?)',
                             (obs_digest, item.source.id, item.boundary_ms, item.observation.decode()))
            if item.receipt:
                receipt_digest = fingerprint(parse(item.receipt))
                old = self._db.execute('SELECT digest,payload FROM source_receipts WHERE observation_digest=?',
                                       (obs_digest,)).fetchone()
                if old and old != (receipt_digest, item.receipt.decode()):
                    raise ValueError('Conflicting source receipt')
                self._db.execute('INSERT OR IGNORE INTO source_receipts VALUES(?,?,?)',
                                 (obs_digest, receipt_digest, item.receipt.decode()))
            self._db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?)',
                             (item.id, encoded(item.record).decode()))
        return item.id
