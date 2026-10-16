"""B4 local durable transitions. Explicit calls only; no worker or activation."""
from contextlib import contextmanager
from copy import deepcopy
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
        'MISSED_ACCEPTANCE_DEADLINE','OPEN','CLOSED','REJECTED_SIZING','REJECTED_EXECUTION','ABANDONED')), payload TEXT NOT NULL);
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
RESOLUTIONS_SCHEMA = '''CREATE TABLE resolutions(owner TEXT PRIMARY KEY REFERENCES reservations(owner),
    digest TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);'''

IMMUTABLE = ('identity', 'journal', 'fills', 'candles', 'processed', 'trades', 'equity')


class LedgerBlocked(ValueError):
    pass


class PaperLedger:
    def __init__(self, directory, binding, rules, source, market, *, fault=None, verified_source=None, authorization=None,
                 invocation_reuse=False, checkpoints=()):
        self._required_checkpoints=deepcopy(tuple(checkpoints))
        if (type(binding) is not AcceptanceBinding or type(rules) is not RuleSnapshot
                or binding.draft.rules_fingerprint != rules.id
                or type(source) is not SourceAdapter or type(market) is not CandleSource
                or source._directory != market._directory
                or source._expected != binding.draft.source or market._expected != binding.draft.source):
            raise ValueError('Explicit bound rules and approved read-only source required')
        if verified_source is not None:
            from paper.verified_source import VerifiedSourceSnapshot
            if (type(verified_source) is not VerifiedSourceSnapshot
                    or verified_source.source != binding.draft.source
                    or verified_source.directory != source._directory):
                raise ValueError('Incorrect invocation source snapshot')
            verified_source.guard()
        self.verified_source = verified_source
        self.authorization = authorization
        self._runtime_identity = implementation()
        provenance = self._runtime_identity
        if authorization is not None:
            from paper.transition import TransitionAuthorization
            from paper.authorization import ClockCapability
            if type(authorization) not in (TransitionAuthorization,ClockCapability) or authorization.binding != binding or authorization.rules != rules:
                raise ValueError('Explicit verified transition authorization required')
            authorization.guard()
            if type(authorization) is ClockCapability:
                from paper.binding import NEW_CYCLE
                if authorization.proposal['kind']==NEW_CYCLE:
                    from research.incremental_full_consumer import IncrementalConsumerLedger
                    if type(self) is not IncrementalConsumerLedger:
                        raise ValueError('New-cycle capability requires incremental ledger')
                from paper.operations import paths
                expected_directory=paths(authorization.record['run_id'],authorization.root)[1]
                if Path(directory).absolute()!=expected_directory:
                    raise ValueError('Capability belongs to another ledger directory')
            provenance = authorization.record['implementations']['b4']
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
        self.identity = dict(label=LABEL, version='paper-b4-ledger-v1', implementation=provenance,
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
            if invocation_reuse:
                if verified_source is None or authorization is None:
                    raise ValueError('Invocation reuse requires bound authorization and detached source')
                self._enable_invocation_reuse()
            self.reconcile()
        except Exception as error:
            try:
                self._block(error)
            finally:
                self.close()
            raise

    def close(self):
        self._invalidate_verified()
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
            self._invalidate_verified()
            try:
                self._db.rollback()
            finally:
                self._block(error)
            raise

    def _invalidate_verified(self):
        self.__verified=None
        self._verification_generation=getattr(self,'_verification_generation',0)+1

    def _enable_invocation_reuse(self):
        """Private connection-local cache. Never serialized or shared with callers.

        Reuse is checked UNDER BEGIN IMMEDIATE: no competing writer can intervene.
        data_version detects commits by other connections since the verified read;
        total_changes and the trace generation detect this connection's writes,
        commits/rollbacks/DDL, even writes subsequently rolled back. An unexpected
        statement invalidates conservatively. Raw connection callback replacement
        or arbitrary private-attribute tampering is outside the runtime API.
        """
        if self._db.in_transaction:raise ValueError('Reuse must start outside a transaction')
        self._reuse=True;self._verification_stats={'full_passes':0,'full_seconds':0.0,'reuse_hits':0}
        self._invalidate_verified()
        def trace(sql):
            head=sql.strip().upper()
            safe=(head.startswith('SELECT ') or head=='BEGIN IMMEDIATE' or
                  head in ('PRAGMA DATA_VERSION','PRAGMA DATABASE_LIST'))
            if not safe:self._invalidate_verified()
        self._db.set_trace_callback(trace)

    def _verification_key(self):
        if not self._db.in_transaction:raise ValueError('Verified state requires a live transaction')
        self._source_guard()
        return (id(self._db),self.identity_id,self.binding.id,self.rules.id,
                fingerprint(self.identity),fingerprint(getattr(self,'_required_checkpoints',())),
                implementation(),getattr(self.authorization,'bound_id',None) if getattr(self,'authorization',None) else None,
                self.verified_source.id if getattr(self,'verified_source',None) else None,
                tuple(self._db.execute('PRAGMA database_list').fetchall()),
                tuple(self._db.execute('SELECT count(*),max(sequence) FROM journal').fetchone()),
                tuple(self._db.execute('SELECT digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchall()),
                self._db.execute('PRAGMA data_version').fetchone()[0],self._db.total_changes,
                self._verification_generation)

    def _verified_state(self):
        if not getattr(self,'_reuse',False):return self._verify()
        try:
            key=self._verification_key()
            saved=self.__verified
            if saved is not None and saved[0]==key:
                self._verification_stats['reuse_hits']+=1
                return deepcopy(saved[1])
            self._invalidate_verified()
            return self._verify()
        except BaseException:
            self._invalidate_verified();raise

    def _commit_verified(self,state):
        """Publish only AFTER successful commit; detect intervening external commit.

        The data_version baseline is sampled before commit, never after a gap in
        ownership. Next reuse acquires BEGIN IMMEDIATE before comparing it.
        """
        if not getattr(self,'_reuse',False):self._db.commit();return
        key=self._verification_key();copy=deepcopy(state)
        self._db.commit()  # trace invalidates the old generation
        self.__verified=(key[:-1]+(self._verification_generation,),copy)

    def _hit(self, point):
        if self.fault is not None:
            self.fault(point)

    @staticmethod
    def _request(state, kind, data):
        if kind == 'ACCEPT':
            return 'ACCEPT:'+str(data['source']['observation']['boundary_ms'])
        owner = data['owner']
        if kind in ('CONFIRM','CONFIRM_V3'):
            return 'CONFIRM:'+owner
        if kind == 'ABANDON_BLOCKED_CLOCK':
            return 'ABANDON_BLOCKED_CLOCK:'+owner
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
            if table == 'resolutions' and not rows:
                continue
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
            self._hit('MATERIALIZE.'+table)

    def _verify(self, *, checkpoints=None):
        start=time.monotonic()
        try:return self._verify_body(checkpoints=checkpoints)
        finally:
            if hasattr(self,'_verification_stats'):
                self._verification_stats['full_passes']+=1
                self._verification_stats['full_seconds']+=time.monotonic()-start

    def _verify_body(self, *, checkpoints=None):
        if implementation() != getattr(self, '_runtime_identity', self.identity['implementation']):
            raise ValueError('B4 implementation changed while ledger was open')
        if (self._db.execute('PRAGMA integrity_check').fetchone() != ('ok',)
                or self._db.execute('PRAGMA foreign_key_check').fetchall()):
            raise ValueError('Paper ledger database integrity failure')
        if self._db.execute('SELECT * FROM identity').fetchall() != [
                (1, self.identity_id, encoded(self.identity).decode())]:
            raise ValueError('Ledger identity changed')
        has_resolutions = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolutions'").fetchone() is not None
        expected_triggers = {f'no_{op}_{table}' for table in IMMUTABLE + (('resolutions',) if has_resolutions else ()) for op in ('UPDATE', 'DELETE')}
        if {r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")} != expected_triggers:
            raise ValueError('Append-only protections changed')
        state = Replay(self.binding, self.rules, self.identity_id)
        checkpoints=tuple(getattr(self,'_required_checkpoints',()) if checkpoints is None else checkpoints)
        lengths=[cp['journal_count'] for cp in checkpoints]
        if any(type(n) is not int or n<0 for n in lengths):raise ValueError('Invalid checkpoint length')
        prefixes={};verified_rows=[]
        def capture_prefix(n):
            if n not in lengths:return
            projections=state.rows()
            prefixes[n]=parse(encoded(dict(genesis=self.identity_id,identity=encoded(self.identity).decode(),
                journal_count=n,journal_fingerprint=fingerprint(verified_rows),
                account=projections['account'],slot=projections['global_slot'])))
        capture_prefix(0)
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
            state.journal_head = digest
            state.event_ids[request] = digest
            verified_rows.append(list(row));capture_prefix(index)
        for table, expected in state.rows().items():
            if table == 'resolutions' and not has_resolutions:
                if expected: raise ValueError('Missing abandonment projection')
                continue
            actual = self._db.execute(f'SELECT * FROM {table}').fetchall()
            if sorted(actual) != sorted(expected):
                raise ValueError('Contradictory durable financial state: '+table)
        if any(prefixes.get(cp['journal_count'])!=cp for cp in checkpoints):
            raise ValueError('Bound historical checkpoint mismatch')
        return state

    def reconcile(self):
        with self._guard():
            reuse=getattr(self,'_reuse',False)
            self._db.execute('BEGIN IMMEDIATE' if reuse else 'BEGIN')
            try:
                state = self._verified_state()
                if reuse:self._commit_verified(state)
                else:self._db.rollback()
                return state
            except BaseException:
                self._invalidate_verified()
                self._db.rollback()
                raise

    def _source_guard(self):
        if getattr(self, 'authorization', None) is not None:
            self.authorization.guard()
        if getattr(self, 'verified_source', None) is not None:
            self.verified_source.guard()

    def _candle(self, symbol, stamp):
        return (self.verified_source.at(symbol, stamp) if self.verified_source is not None
                else self.market.at(symbol, stamp))

    def _sources(self, state):
        self._source_guard()
        snapshot = self.verified_source.snapshot() if self.verified_source is not None else self.source.snapshot()
        items = {o.boundary_ms: o for o in snapshot}
        if len(items) != len(snapshot) or any(o.source != self.binding.draft.source for o in snapshot):
            raise ValueError('Duplicate or incorrectly bound source batch')
        for boundary, batch in state.batches.items():
            old = batch['source']; current = items.get(boundary)
            if (current is None or current.observation != encoded(old['observation'])
                    or old['receipt'] is not None and current.receipt != encoded(old['receipt'])):
                raise ValueError('Previously accepted source observation/receipt changed')
        for value in state.candles.values():
            c = value['candle']; current = self._candle(c['symbol'], c['open_time_ms'])
            if current is None or current.record != value:
                raise ValueError('Previously consumed source candle changed')
        return items

    def _append(self, kind, data, request, *, bar_open_ms=None):
        self._source_guard()
        self._hit(kind+'.before')
        # SQLite table rebuild requires FK enforcement off outside the transaction.
        # The ledger's exclusive lock is held; foreign_key_check runs before commit.
        migrating = kind == 'ABANDON_BLOCKED_CLOCK'
        if migrating: self._db.execute('PRAGMA foreign_keys=OFF')
        try:
            self._db.execute('BEGIN IMMEDIATE')
            state = self._verified_state()
            if bar_open_ms is not None:
                # Derive the next BAR from this transaction's verified state,
                # not a replay from an earlier read transaction. No state cache
                # or caller-supplied verification token crosses this boundary.
                if kind != 'BAR' or request is not None or set(data) != {'owner'}:
                    raise ValueError('Invalid transactional BAR request')
                timestamp(bar_open_ms)
                self._sources(state)
                owner = data['owner']
                r = state.reservations[owner]['reservation']
                item = self._candle(r['symbol'], bar_open_ms)
                position = state.positions[owner]
                expected = position.last_processed_ms+900000 if position.last_processed_ms is not None else r['entry_boundary_ms']
                request = 'BAR:'+owner+':'+str(bar_open_ms)
                if self._db.execute('SELECT 1 FROM journal WHERE request=?', (request,)).fetchone() is None and bar_open_ms != expected:
                    raise ValueError('Must process exact next candle')
                if item is None:
                    request = 'WAIT:'+request
                data = dict(owner=owner, candle=item.record if item else None)
            row = self._db.execute('SELECT payload FROM journal WHERE request=?', (request,)).fetchone()
            if row:
                old = parse(row[0].encode())
                if old['kind'] != kind or old['input'] != data:
                    raise ValueError('Conflicting transition retry')
                self._db.rollback()
                return old['result']
            if request != self._request(state, kind, data):
                raise ValueError('Incorrect transition request')
            self._invalidate_verified()
            result = state.apply(kind, data)
            tail = self._db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
            sequence, parent = (tail[0]+1, tail[1]) if tail else (1, self.identity_id)
            body = dict(label=LABEL, parent=parent, request=request, kind=kind, input=data, result=result)
            self._db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',
                             (sequence, fingerprint(body), request, parent, encoded(body).decode()))
            self._hit(kind+'.journal')
            if migrating:
                sql = self._db.execute("SELECT sql FROM sqlite_master WHERE name='reservations'").fetchone()[0]
                if "'ABANDONED'" not in sql:
                    expected = SCHEMA.split('CREATE TABLE reservations',1)[1].split(';',1)[0]
                    legacy = ('CREATE TABLE reservations'+expected).replace(",'ABANDONED'",'')
                    if sql != legacy: raise ValueError('Unknown historical reservation schema')
                    self._db.execute('CREATE TABLE reservations_v3'+expected)
                    self._db.execute('INSERT INTO reservations_v3 SELECT * FROM reservations')
                    self._db.execute('DROP TABLE reservations')
                    self._db.execute('ALTER TABLE reservations_v3 RENAME TO reservations')
                    self._db.execute("CREATE UNIQUE INDEX one_active_exposure ON reservations((1)) WHERE state IN ('UNCONFIRMED_ACCEPTANCE','PENDING_ENTRY','BLOCKED_CLOCK','MISSED_ACCEPTANCE_DEADLINE','OPEN')")
                    self._hit(kind+'.reservation_schema')
            if kind == 'ABANDON_BLOCKED_CLOCK' and not self._db.execute("SELECT 1 FROM sqlite_master WHERE name='resolutions'").fetchone():
                self._db.execute(RESOLUTIONS_SCHEMA)
                for op in ('UPDATE','DELETE'):
                    self._db.execute(f"CREATE TRIGGER no_{op}_resolutions BEFORE {op} ON resolutions BEGIN SELECT RAISE(ABORT,'Append only'); END")
                self._hit(kind+'.schema')
            self._materialize(state)
            self._hit(kind+'.state')
            verified_post = self._verify()
            self._hit(kind+'.before_commit')
            # Fault hooks may not alter the post-verified database silently.
            # Production has no fault hook; tests use it only for crash injection.
            if self.fault is not None:verified_post=self._verify()
            self._commit_verified(verified_post)
        except BaseException:
            self._invalidate_verified()
            self._db.rollback()
            raise
        finally:
            if migrating: self._db.execute('PRAGMA foreign_keys=ON')
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
            from paper.authorization import ClockCapability
            if type(self.authorization) is ClockCapability: raise ValueError('Successor requires prospective v3 dispatch')
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
            item = self._candle(r['symbol'], r['entry_boundary_ms'])
            request = 'ENTRY:'+owner if item is not None else 'WAIT:ENTRY:'+owner
            return self._append('ENTRY', dict(owner=owner, candle=item.record if item else None), request)

    def process_candle(self, owner, open_ms):
        with self._guard():
            timestamp(open_ms)
            return self._append('BAR', dict(owner=owner), None, bar_open_ms=open_ms)

    def accept_v3(self, boundary_ms, *, authorization):
        """Prospective entry point requiring a canonical bound successor capability."""
        from paper.clock_v3 import sample, valid_sample
        from paper.authorization import ClockCapability
        if type(authorization) is not ClockCapability:
            raise ValueError('Explicit clock-v3 authorization required')
        authorization.guard()
        if authorization.binding!=self.binding or self.authorization is not authorization:
            raise ValueError('Ledger capability mismatch')
        with self._guard():
            state = self.reconcile()
            items = self._sources(state)
            timestamp(boundary_ms)
            if boundary_ms in state.batches:
                return state.result(boundary_ms)
            a = sample()
            if not valid_sample(a) or a['before_ns'] > a['after_ns'] or a['after_ns']-a['before_ns'] > 5000000:
                raise ValueError('Invalid pre-acceptance clock bracket')
            authorization.check(self.binding,a['utc_ms'],boundary_ms)
            if self.authorization is not authorization: raise ValueError('Ledger capability mismatch')
            result = self._append('ACCEPT',dict(source=items[boundary_ms].record,
                accepted_ms=a['utc_ms'],equity=state.basis),'ACCEPT:'+str(boundary_ms))
            if result['reservation']:
                c = sample()
                owner = result['reservation']['reservation_id']
                result = self._append('CONFIRM_V3',dict(owner=owner,acceptance=a,durable=c,
                    authorization=authorization.proposal['clock'],approval=fingerprint(authorization.proposal['clock']),
                    binding_evidence=authorization.envelope),'CONFIRM:'+owner)
            return result

    def abandon_blocked_clock(self, authorization):
        """Operator-only explicit authorization. Never called by the consumer."""
        from paper.authorization import AbandonCapability
        if type(authorization) is not AbandonCapability:
            raise ValueError('Explicit exact abandonment authorization required')
        authorization.guard()
        if self.authorization is not authorization.successor: raise ValueError('Resolution capability mismatch')
        owner = authorization.record['checkpoint']['owner']
        with self._guard():
            return self._append('ABANDON_BLOCKED_CLOCK',dict(owner=owner,
                authorization=authorization.record,approval=authorization.approved_fingerprint,
                binding_evidence=authorization.envelope,successor_evidence=authorization.successor.envelope),
                'ABANDON_BLOCKED_CLOCK:'+owner)
