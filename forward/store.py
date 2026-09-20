"""Separate durable append-only observation store. No live trading DB imports."""
import json
import sqlite3
from pathlib import Path

from backtest.data import Candle, SYMBOLS, STEP_MS
from research.spec import encoded, fingerprint
from runtime import DatabaseLock
from forward.model import FOUR_HOURS, HISTORY, LABEL


class Store:
    def __init__(self, directory, manifest):
        self.manifest = manifest
        path = Path(directory)/'observer.sqlite'
        self.lock = DatabaseLock(path)
        self.lock.acquire()
        try:
            self.db = sqlite3.connect(path)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candles (symbol TEXT NOT NULL, open_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL, received_ms INTEGER NOT NULL,
                    PRIMARY KEY(symbol,open_ms));
                CREATE TABLE IF NOT EXISTS observations (boundary_ms INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL);
            ''')
            for table in ('identity','candles','observations','events'):
                for operation in ('UPDATE','DELETE'):
                    self.db.execute(f'''CREATE TRIGGER IF NOT EXISTS no_{operation}_{table}
                        BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END''')
            row = self.db.execute('SELECT fingerprint FROM identity').fetchone()
            if row is None:
                if any(self.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('candles','observations','events')):
                    raise ValueError('Missing database identity')
                self.db.execute('INSERT INTO identity VALUES(1,?)', (manifest['manifest_fingerprint'],))
                self.db.commit()
            elif row[0] != manifest['manifest_fingerprint']:
                raise ValueError('Database belongs to another manifest')
            self.verify()
        except Exception:
            self.close()
            raise

    def close(self):
        if hasattr(self,'db'): self.db.close()
        self.lock.release()

    def verify(self):
        if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Observation database integrity failure')
        expected = {s:self.manifest['history_start_ms'] for s in SYMBOLS}
        for s,t,raw,digest,received in self.db.execute('SELECT * FROM candles ORDER BY symbol,open_ms'):
            c = Candle(**json.loads(raw))
            if (s not in expected or t != expected[s] or c.symbol != s or c.open_time_ms != t
                    or raw != encoded(c.record()).decode() or fingerprint(c.record()) != digest
                    or received <= c.close_time_ms):
                raise ValueError('Corrupt or discontinuous durable candles')
            expected[s] += STEP_MS
        boundary = self.manifest['first_observation_ms']
        for t,raw,digest in self.db.execute('SELECT * FROM observations ORDER BY boundary_ms'):
            record = json.loads(raw)
            if (t != boundary or t > min(expected.values()) or record['boundary_ms'] != t
                    or record['manifest_fingerprint'] != self.manifest['manifest_fingerprint']
                    or encoded(record).decode() != raw or fingerprint(record) != digest):
                raise ValueError('Corrupt observation cursor')
            boundary += FOUR_HOURS

    def next_candle(self, symbol):
        if symbol not in SYMBOLS: raise ValueError('Unsupported symbol')
        value = self.db.execute('SELECT max(open_ms) FROM candles WHERE symbol=?', (symbol,)).fetchone()[0]
        return self.manifest['history_start_ms'] if value is None else value+STEP_MS

    def append(self, symbol, candles, *, received_ms):
        cs = tuple(candles)
        with self.db:
            cursor = self.next_candle(symbol)
            for c in cs:
                if type(c) is not Candle or c.symbol != symbol or c.close_time_ms >= received_ms:
                    raise ValueError('Only normalized closed candles can be persisted')
                raw = encoded(c.record()).decode()
                old = self.db.execute('SELECT payload FROM candles WHERE symbol=? AND open_ms=?', (symbol,c.open_time_ms)).fetchone()
                if old:
                    if old[0] != raw: raise ValueError('Conflicting candle revision')
                    continue  # Identical transport redelivery is idempotent.
                if c.open_time_ms != cursor: raise ValueError('Candle gap or unexpected first candle')
                self.db.execute('INSERT INTO candles VALUES(?,?,?,?,?)',
                    (symbol,c.open_time_ms,raw,fingerprint(c.record()),received_ms))
                cursor += STEP_MS

    def history(self, symbol, boundary):
        rows = self.db.execute('SELECT payload FROM candles WHERE symbol=? AND open_ms>=? AND open_ms<? ORDER BY open_ms',
                               (symbol,boundary-HISTORY,boundary))
        return tuple(Candle(**json.loads(row[0])) for row in rows)

    def next_boundary(self):
        value = self.db.execute('SELECT max(boundary_ms) FROM observations').fetchone()[0]
        return self.manifest['first_observation_ms'] if value is None else value+FOUR_HOURS

    def observation(self, record, *, clock=None, operations=None):
        if clock is not None:
            from forward.timing import classify
            ready=clock.now()
            classify(record['boundary_ms'],ready)  # Reject before any write.
            record=dict(record,observed_ms=ready,decision_ready_ms=ready,timing='UNCONFIRMED',
                        operations_fingerprint=operations['operations_fingerprint'])
        with self.db:
            if record['boundary_ms'] != self.next_boundary():
                raise ValueError('Duplicate or noncontiguous observation')
            self.db.execute('INSERT INTO observations VALUES(?,?,?)',
                (record['boundary_ms'],encoded(record).decode(),fingerprint(record)))
            self.db.execute('INSERT INTO events(payload) VALUES(?)',
                (encoded(dict(label=LABEL,event='OBSERVATION',fingerprint=fingerprint(record))).decode(),))
        if clock is not None:
            # This sample is AFTER the FULL synchronous observation commit.
            persisted=clock.now()
            tolerance=(operations['policy']['timely_ms'] if record['boundary_ms']>=operations['effective_boundary_ms']
                       else self.manifest['spec']['max_online_lateness_ms'])
            self.event('RECEIPT',observed_ms=persisted,boundary_ms=record['boundary_ms'],
                observation_fingerprint=fingerprint(record),timing=classify(record['boundary_ms'],persisted,tolerance),
                operations_fingerprint=operations['operations_fingerprint'],tolerance_ms=tolerance)

    def event(self, name, **metadata):
        if name not in ('START','STOP','PUBLIC_ERROR','OBSERVATION_ERROR','BLOCKED','RECOVERABLE',
                        'RECEIPT','RECOVERED_RECEIPT','BACKOFF'):
            raise ValueError('Only sanitized event codes')
        if set(metadata)-{'observed_ms','boundary_ms','observation_fingerprint','timing',
                          'operations_fingerprint','tolerance_ms','retry_not_before_ms','code'}:
            raise ValueError('Unrecognized event metadata')
        with self.db:
            self.db.execute('INSERT INTO events(payload) VALUES(?)', (encoded(dict(label=LABEL,event=name,**metadata)).decode(),))
