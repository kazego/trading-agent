"""B4 local durable transitions. Explicit calls only; no worker or activation."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import time

from paper.acceptance import AcceptanceBinding, timestamp
from paper.market import RuleSnapshot, CandleSource
from paper.model import LABEL, parse
from paper.source import SourceAdapter
from paper.journal import Replay, HISTORICAL, TemporalConsistencyError, implementation
from research.spec import encoded, fingerprint
from runtime import DatabaseLock


SCHEMA = '''
CREATE TABLE identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE journal(sequence INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, request TEXT NOT NULL UNIQUE,
                     parent TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE reservations(owner TEXT PRIMARY KEY, boundary_ms INTEGER NOT NULL UNIQUE,
    symbol TEXT NOT NULL CHECK(symbol IN ('BTCUSDT','ETHUSDT','ADAUSDT')),
    state TEXT NOT NULL CHECK(state IN ('UNCONFIRMED_ACCEPTANCE','PENDING_ENTRY','BLOCKED_CLOCK',
        'MISSED_ACCEPTANCE_DEADLINE','OPEN','CLOSED','REJECTED_SIZING','REJECTED_EXECUTION')), payload TEXT NOT NULL);
CREATE UNIQUE INDEX one_active_exposure ON reservations((1)) WHERE state IN
    ('UNCONFIRMED_ACCEPTANCE','PENDING_ENTRY','BLOCKED_CLOCK','MISSED_ACCEPTANCE_DEADLINE','OPEN');
CREATE TABLE positions(owner TEXT PRIMARY KEY REFERENCES reservations(owner), digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE TABLE fills(owner TEXT NOT NULL REFERENCES reservations(owner), leg TEXT NOT NULL CHECK(leg IN ('ENTRY','EXIT')),
    digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, PRIMARY KEY(owner,leg));
CREATE TABLE candles(digest TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE processed(owner TEXT NOT NULL REFERENCES positions(owner), open_ms INTEGER NOT NULL,
    candle_id TEXT NOT NULL REFERENCES candles(digest), PRIMARY KEY(owner,open_ms));
CREATE TABLE trades(owner TEXT PRIMARY KEY REFERENCES positions(owner), digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
CREATE TABLE equity(sequence INTEGER PRIMARY KEY, owner TEXT NOT NULL UNIQUE REFERENCES trades(owner),
    digest TEXT NOT NULL UNIQUE, before_amount TEXT NOT NULL CHECK(typeof(before_amount)='text'),
    delta TEXT NOT NULL CHECK(typeof(delta)='text'), after_amount TEXT NOT NULL CHECK(typeof(after_amount)='text'),
    available_at_ms INTEGER NOT NULL CHECK(typeof(available_at_ms)='integer'), payload TEXT NOT NULL);
CREATE TABLE global_slot(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    phase TEXT NOT NULL CHECK(phase IN ('EMPTY','PENDING','OPEN','BLOCKED')), owner TEXT REFERENCES reservations(owner), symbol TEXT,
    CHECK((phase='EMPTY' AND owner IS NULL AND symbol IS NULL) OR
          (phase!='EMPTY' AND owner IS NOT NULL AND symbol IS NOT NULL)));
CREATE TABLE account(singleton INTEGER PRIMARY KEY CHECK(singleton=1), amount TEXT NOT NULL CHECK(typeof(amount)='text'), head TEXT NOT NULL,
    financial_available_ms INTEGER CHECK(financial_available_ms IS NULL OR typeof(financial_available_ms)='integer'),
    state_available_ms INTEGER CHECK(state_available_ms IS NULL OR typeof(state_available_ms)='integer'));
'''
IMMUTABLE = ('identity', 'journal', 'fills', 'candles', 'processed', 'trades', 'equity')


class LedgerBlocked(ValueError):
    pass


class PaperLedger:
    def __init__(self, directory, binding, rules, source, market, *, fault=None):
        if (type(binding) is not AcceptanceBinding or type(rules) is not RuleSnapshot
                or binding.draft.rules_fingerprint != rules.id
                or type(source) is not SourceAdapter or type(market) is not CandleSource
                or source._directory != market._directory
                or source._expected != binding.draft.source or market._expected != binding.draft.source):
            raise ValueError('Explicit bound rules and approved read-only source required')
        directory = Path(directory).absolute()
        if (directory.resolve() != directory or directory.parent.name != 'forward-paper-execution'
                or directory.parent.parent.name != 'artifacts' or not directory.name.startswith('b4-')):
            raise ValueError('B4 foundation namespace only; activated runs are forbidden')
        path = directory/'paper.sqlite'
        if path.is_symlink() or path.exists() and path.stat().st_nlink != 1:
            raise ValueError('Aliased database forbidden')
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.binding, self.rules = directory, binding, rules
        self.source, self.market, self.fault = source, market, fault
        self.identity = dict(label=LABEL, version='paper-b4-ledger-v1', implementation=implementation(),
            historical=HISTORICAL, binding=binding.record, rules=rules.record,
            activation='UNSET_FOUNDATION_ONLY', initial_equity='1000',
            initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',
                                             available_at_ms=binding.prospective.start_ms))
        self.identity_id = fingerprint(self.identity)
        self._lock = DatabaseLock(path)
        self._lock.acquire()
        try:
            self._db = sqlite3.connect(path)
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.execute('PRAGMA synchronous=FULL')
            self._check_latch()
            if not self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                self._db.executescript('BEGIN IMMEDIATE;\n'+SCHEMA)
                for table in IMMUTABLE:
                    for op in ('UPDATE', 'DELETE'):
                        self._db.execute(f"CREATE TRIGGER no_{op}_{table} BEFORE {op} ON {table} "
                                         "BEGIN SELECT RAISE(ABORT,'Append only'); END")
                self._db.execute('INSERT INTO identity VALUES(1,?,?)',
                                 (self.identity_id, encoded(self.identity).decode()))
                self._materialize(Replay(binding, rules, self.identity_id))
                self._db.commit()
            self.reconcile()
        except Exception as error:
            try:
                self._block(error)
            finally:
                self.close()
            raise

    def close(self):
        if hasattr(self, '_db'):
            self._db.close()
        self._lock.release()

    def _check_latch(self):
        if (self.directory/'BLOCKED.json').exists():
            raise LedgerBlocked('B4 requires explicit investigation; never automatically clears BLOCKED')

    def _block(self, error=None):
        # Independent durable audit latch still works if the financial DB is corrupt.
        body = dict(label=LABEL, state='BLOCKED', code='INTEGRITY_OR_SOURCE_FAILURE', identity=self.identity_id)
        if isinstance(error, TemporalConsistencyError):
            body.update(code='TEMPORAL_EVIDENCE_REGRESSION', temporal_evidence=error.audit)
        try:
            with (self.directory/'BLOCKED.json').open('xb') as handle:
                handle.write(encoded(body)+b'\n'); handle.flush(); os.fsync(handle.fileno())
        except FileExistsError:
            pass

    @contextmanager
    def _guard(self):
        self._check_latch()
        try:
            yield
        except Exception as error:
            try:
                self._db.rollback()
            finally:
                self._block(error)
            raise

    def _hit(self, point):
        if self.fault is not None:
            self.fault(point)

    @staticmethod
    def _request(state, kind, data):
        if kind == 'ACCEPT':
            return 'ACCEPT:'+str(data['source']['observation']['boundary_ms'])
        owner = data['owner']
        if kind == 'CONFIRM':
            return 'CONFIRM:'+owner
        prefix = 'WAIT:' if data['candle'] is None else ''
        if kind == 'ENTRY':
            return prefix+'ENTRY:'+owner
        if kind != 'BAR':
            raise ValueError('Unknown journal operation')
        p = state.positions[owner]
        expected = p.entry.candle_open_time_ms if p.last_processed_ms is None else p.last_processed_ms+900000
        stamp = data['candle']['candle']['open_time_ms'] if data['candle'] is not None else expected
        return prefix+'BAR:'+owner+':'+str(stamp)

    def _materialize(self, state):
        for table, rows in state.rows().items():
            for row in rows:
                placeholders = ','.join('?' for _ in row)
                if table == 'reservations':
                    self._db.execute(f'INSERT INTO {table} VALUES({placeholders}) ON CONFLICT(owner) '
                                     'DO UPDATE SET state=excluded.state', row)
                elif table == 'positions':
                    self._db.execute(f'INSERT INTO {table} VALUES({placeholders}) ON CONFLICT(owner) '
                                     'DO UPDATE SET digest=excluded.digest,payload=excluded.payload', row)
                elif table in ('global_slot', 'account'):
                    cols = ('phase,owner,symbol' if table == 'global_slot'
                            else 'amount,head,financial_available_ms,state_available_ms').split(',')
                    updates = ','.join(f'{c}=excluded.{c}' for c in cols)
                    self._db.execute(f'INSERT INTO {table} VALUES({placeholders}) ON CONFLICT(singleton) '
                                     f'DO UPDATE SET {updates}', row)
                else:
                    self._db.execute(f'INSERT OR IGNORE INTO {table} VALUES({placeholders})', row)

    def _verify(self):
        if implementation() != self.identity['implementation']:
            raise ValueError('B4 implementation changed while ledger was open')
        if (self._db.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or self._db.execute('PRAGMA foreign_key_check').fetchall()):
            raise ValueError('Paper ledger database integrity failure')
        if self._db.execute('SELECT * FROM identity').fetchall() != [
                (1, self.identity_id, encoded(self.identity).decode())]:
            raise ValueError('Ledger identity changed')
        expected_triggers = {f'no_{op}_{table}' for table in IMMUTABLE for op in ('UPDATE', 'DELETE')}
        if {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")} != expected_triggers:
            raise ValueError('Append-only protections changed')
        state = Replay(self.binding, self.rules, self.identity_id)
        previous = self.identity_id
        for index, row in enumerate(self._db.execute('SELECT * FROM journal ORDER BY sequence'), 1):
            sequence, digest, request, parent, raw = row
            event = parse(raw.encode())
            if sequence != index or parent != previous or event['parent'] != parent or event['request'] != request:
                raise ValueError('Broken journal sequence/ancestry')
            if request != self._request(state, event['kind'], event['input']):
                raise ValueError('Incorrect deterministic request identity')
            result = state.apply(event['kind'], event['input'])
            expected = dict(label=LABEL, parent=parent, request=request, kind=event['kind'],
                            input=event['input'], result=result)
            if expected != event or fingerprint(expected) != digest:
                raise ValueError('Journal financial replay mismatch')
            previous = digest
        for table, expected in state.rows().items():
            actual = self._db.execute(f'SELECT * FROM {table}').fetchall()
            if sorted(actual) != sorted(expected):
                raise ValueError('Contradictory durable financial state: '+table)
        return state

    def reconcile(self):
        with self._guard():
            self._db.execute('BEGIN')
            try:
                state = self._verify()
                self._db.rollback()
                return state
            except BaseException:
                self._db.rollback()
                raise

    def _sources(self, state):
        snapshot = self.source.snapshot()
        items = {o.boundary_ms: o for o in snapshot}
        if len(items) != len(snapshot) or any(o.source != self.binding.draft.source for o in snapshot):
            raise ValueError('Duplicate or incorrectly bound source batch')
        for boundary, batch in state.batches.items():
            old = batch['source']; current = items.get(boundary)
            if (current is None or current.observation != encoded(old['observation'])
                    or old['receipt'] is not None and current.receipt != encoded(old['receipt'])):
                raise ValueError('Previously accepted source observation/receipt changed')
        for value in state.candles.values():
            c = value['candle']; current = self.market.at(c['symbol'], c['open_time_ms'])
            if current is None or current.record != value:
                raise ValueError('Previously consumed source candle changed')
        return items

    def _append(self, kind, data, request):
        self._hit(kind+'.before')
        self._db.execute('BEGIN IMMEDIATE')
        try:
            state = self._verify()
            row = self._db.execute('SELECT payload FROM journal WHERE request=?', (request,)).fetchone()
            if row:
                old = parse(row[0].encode())
                if old['kind'] != kind or old['input'] != data:
                    raise ValueError('Conflicting transition retry')
                self._db.rollback()
                return old['result']
            if request != self._request(state, kind, data):
                raise ValueError('Incorrect transition request')
            result = state.apply(kind, data)
            tail = self._db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
            sequence, parent = (tail[0]+1, tail[1]) if tail else (1, self.identity_id)
            body = dict(label=LABEL, parent=parent, request=request, kind=kind, input=data, result=result)
            self._db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',
                             (sequence, fingerprint(body), request, parent, encoded(body).decode()))
            self._hit(kind+'.journal')
            self._materialize(state)
            self._hit(kind+'.state')
            self._verify()
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        self._hit(kind+'.committed')
        return result

    def accept(self, boundary_ms, *, clock):
        """Explicit current-time acceptance, never replayed or inferred from prices."""
        with self._guard():
            state = self.reconcile()
            items = self._sources(state)
            timestamp(boundary_ms)
            item = items[boundary_ms]
            if item.source != self.binding.draft.source:
                raise ValueError('Wrong source binding')
            if boundary_ms in state.batches:
                return state.result(boundary_ms)  # Never confirm an interrupted acceptance later.
            start = time.monotonic_ns()
            if type(start) is not int or start < 0:
                raise ValueError('Invalid monotonic sample')
            data = dict(source=item.record, accepted_ms=timestamp(clock()), equity=state.basis)
            result = self._append('ACCEPT', data, 'ACCEPT:'+str(boundary_ms))
            r = result['reservation']
            if r:
                end = time.monotonic_ns()
                elapsed = end-start if type(end) is int else None
                result = self._append('CONFIRM', dict(owner=r['reservation_id'], durable_ms=clock(), elapsed_ns=elapsed),
                                      'CONFIRM:'+r['reservation_id'])
            return result

    def settle_entry(self, owner):
        with self._guard():
            state = self.reconcile(); self._sources(state)
            r = state.reservations[owner]['reservation']
            item = self.market.at(r['symbol'], r['entry_boundary_ms'])
            request = 'ENTRY:'+owner if item is not None else 'WAIT:ENTRY:'+owner
            return self._append('ENTRY', dict(owner=owner, candle=item.record if item else None), request)

    def process_candle(self, owner, open_ms):
        with self._guard():
            timestamp(open_ms)
            state = self.reconcile(); self._sources(state)
            r = state.reservations[owner]['reservation']
            item = self.market.at(r['symbol'], open_ms)
            position = state.positions[owner]
            expected = position.last_processed_ms+900000 if position.last_processed_ms is not None else r['entry_boundary_ms']
            request = 'BAR:'+owner+':'+str(open_ms)
            if self._db.execute('SELECT 1 FROM journal WHERE request=?', (request,)).fetchone() is None and open_ms != expected:
                raise ValueError('Must process exact next candle')
            if item is None:
                request = 'WAIT:'+request
            return self._append('BAR', dict(owner=owner, candle=item.record if item else None), request)
