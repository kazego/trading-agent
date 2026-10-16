"""Experimental incremental core: in-memory synthetic SQLite ONLY.

No operational loader, filesystem writer, consumer or migration entry point.
Replay.apply is the sole financial reducer. ExternalMemory models a separate
trusted continuity/audit store; it is NOT a production authorization mechanism.
Filesystem durability and production trust integration remain unimplemented.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from functools import wraps
from threading import RLock
import time

from paper.journal import Replay, candle, implementation
from paper.ledger import PaperLedger
from paper.model import LABEL, parse
from paper.settlement import settle
from research.spec import encoded, fingerprint


VERSION = 'synthetic-incremental-v1'
KEYS = dict(reservations=1, positions=1, fills=2, candles=1, processed=2,
            trades=1, equity=1, global_slot=1, account=1, resolutions=1)
MUTABLE = {'reservations', 'positions', 'global_slot', 'account'}


class Blocked(ValueError):
    pass


def exclusive(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.external.lock:
            return method(self,*args,**kwargs)
    return call


def compact(state):
    """Bounded continuation, not a serialized historical Replay.

    One maximum-receipt witness suffices for Replay's financial availability max.
    It retains the actual processed key/evidence, never a fabricated candle.
    """
    owner = state.slot[1]
    boundaries = {max(state.batches)} if state.batches else set()
    if owner:
        boundaries.add(state.reservations[owner]['reservation']['signal_boundary_ms'])
    position = state.positions.get(owner)
    witnesses = [(key, state.candles[value]) for key, value in state.processed.items()
                 if key[0] == owner]
    witness = max(witnesses, key=lambda pair: (pair[1]['received_ms'], pair[0])) if witnesses else None
    return parse(encoded(dict(
        version=VERSION, identity=state.identity, binding=state.binding.id, rules=state.rules.id,
        head=state.journal_head, slot=state.slot, equity=state.equity,
        accounting_head=state.accounting_head, financial_ms=state.financial_available_ms,
        state_ms=state.state_available_ms,
        batches=[[n, state.batches[n]] for n in sorted(boundaries)],
        reservation=state.reservations.get(owner), confirmation=state.confirmations.get(owner),
        position=(dict(entry=position.entry_evidence.record, cursor=position.last_processed_ms,
                       history=position.history_digest) if position else None),
        witness=witness,
        identities={key: value for key, value in state.event_ids.items()
                    if owner and key in ('ACCEPT:'+str(state.reservations[owner]['reservation']['signal_boundary_ms']),
                                         'CONFIRM:'+owner)})))


def restore(value, binding, rules, genesis):
    if (value['version'], value['identity'], value['binding'], value['rules']) != (
            VERSION, genesis, binding.id, rules.id):
        raise Blocked('Continuation identity mismatch')
    s = Replay(binding, rules, genesis)
    s.journal_head = value['head']; s.slot = tuple(value['slot'])
    s.equity = Decimal(value['equity']); s.accounting_head = value['accounting_head']
    s.financial_available_ms = value['financial_ms']; s.state_available_ms = value['state_ms']
    s.batches = dict(value['batches']); s.event_ids = dict(value['identities'])
    owner = s.slot[1]
    if owner:
        s.reservations[owner] = deepcopy(value['reservation'])
        if value['confirmation'] is not None:s.confirmations[owner] = deepcopy(value['confirmation'])
        if value['position'] is not None:
            p = value['position']; evidence = candle(binding.draft.source, p['entry'])
            original = settle(s.instruction(owner), evidence, rules).position
            if original is None:raise Blocked('Invalid active entry')
            s.positions[owner] = replace(original, last_processed_ms=p['cursor'], history_digest=p['history'])
            # ENTRY already persisted this exact evidence, even before any BAR.
            s.candles[evidence.id] = evidence.record
        if value['witness'] is not None:
            key, evidence = value['witness']; item = candle(binding.draft.source, evidence)
            s.processed[tuple(key)] = item.id; s.candles[item.id] = evidence
    if compact(s) != value:raise Blocked('Noncanonical continuation')
    return s


class ExternalMemory:
    """Test double for independently owned, create-once continuity records.

    Never derive trust from a hash supplied by a runtime caller. Only bootstrap
    after full verification and independent audit may establish this test store.
    No claim of resistance to an administrator replacing both stores.
    """
    def __init__(self):
        self.__records = []; self.__cursors = {}; self.audit = None; self.blocked = False
        self.lock = RLock()  # Test-process ownership; not an OS/process lock.
        self.cursor_reads = []  # Experimental instrumentation, sequence numbers only.

    @property
    def blocked(self):return self.__blocked

    @blocked.setter
    def blocked(self,value):
        if value:self.invalidate_cursors()
        self.__blocked=value

    @property
    def lock(self):return self.__lock

    @lock.setter
    def lock(self,value):
        self.invalidate_cursors()
        self.__lock=value

    @property
    def _records(self):
        # Immutable bytes in an immutable view: ordinary callers cannot edit a prefix.
        return tuple(self.__records)

    @_records.setter
    def _records(self, values):
        # Kept for corruption fixtures; replacement ALWAYS revokes every cursor.
        self.invalidate_cursors()
        self.__records = list(values)

    def invalidate_cursors(self):
        self.__cursors.clear()

    def issue_cursor(self, anchor, owner, session, snapshot):
        with self.lock:
            if self.blocked:raise Blocked('Continuity blocked')
            records=[parse(raw) for raw in self.__records]
            keys=('version','reducer','authorization','schema','policy')
            for previous,current in zip(records,records[1:]):
                if (current['sequence']!=previous['sequence']+1
                        or any(current[k]!=previous[k] for k in keys)
                        or current['state']['identity']!=previous['state']['identity']):
                    self.invalidate_cursors();raise Blocked('Initial continuity divergence')
            matches=[i for i,value in enumerate(records) if value==anchor]
            if len(matches)!=1:raise Blocked('Cursor prefix missing/ambiguous')
            token=object()
            self.__cursors[token]=(matches[0],encoded(anchor),owner,session,snapshot,self.lock)
            return token

    def cursor_suffix(self, token, anchor, owner, session, snapshot):
        with self.lock:
            saved=self.__cursors.pop(token,None) if type(token) is object else None
            if saved is None:raise Blocked('Unknown or consumed continuity cursor')
            index,raw,who,sid,snap,lock=saved
            if (self.blocked or who is not owner or sid!=session or snap!=snapshot or lock is not self.lock
                    or raw!=encoded(anchor) or index>=len(self.__records) or self.__records[index]!=raw):
                self.invalidate_cursors();raise Blocked('Invalid continuity cursor ownership/state')
            # Anchor N comes from the verified cursor. Only N+1..M is decoded.
            suffix=[]
            for item in self.__records[index+1:]:
                value=parse(item);self.cursor_reads.append(value['sequence']);suffix.append(value)
            return [parse(raw),*suffix]

    @property
    def anchor(self):
        return parse(self.__records[-1]) if self.__records else None

    @property
    def records(self):
        return tuple(self.__records)

    def publish(self, previous, current):
        with self.lock:
            try:
                if self.blocked or self.anchor != previous:raise Blocked('Continuity CAS mismatch')
                if previous is not None and current['sequence'] != previous['sequence']+1:
                    raise Blocked('Continuity sequence mismatch')
                self.__records.append(encoded(current))
            except BaseException:
                self.invalidate_cursors();raise


class SyntheticLedger:
    def __init__(self, verifier, external, *, authorization_id, authorize, evidence,
                 clock=time.time, max_age=86400, max_events=1000):
        self.v = verifier; self.db = verifier._db; self.external = external
        # Refuse all disk databases and attachments, before any write.
        self._validate_database()
        if self.db.in_transaction:raise Blocked('Caller transaction forbidden')
        if type(max_age) is not int or max_age <= 0 or type(max_events) is not int or max_events <= 0:
            raise ValueError('Positive provisional audit limits required')
        self.authorization_id = authorization_id; self.authorize = authorize
        self.evidence = evidence; self.clock = clock
        self.policy = dict(max_age=max_age, max_events=max_events)
        self.reducer = implementation()

    def _validate_database(self):
        databases = self.db.execute('PRAGMA database_list').fetchall()
        if any(name not in ('main', 'temp') or filename for _, name, filename in databases):
            raise Blocked('Synthetic in-memory database only')

    def _guard(self):
        if self.external.blocked:raise Blocked('Independent audit/continuity blocked')
        if implementation() != self.reducer:raise Blocked('Reducer changed')
        if self.authorize() != self.authorization_id:raise Blocked('Authorization changed')

    def _envelope(self, state, n, financial_count):
        return dict(version=VERSION, reducer=self.reducer, authorization=self.authorization_id,
                    schema=self._schema(), policy=self.policy, sequence=n,
                    financial_count=financial_count, state=compact(state))

    def _schema(self):
        return fingerprint(self.db.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall())

    @exclusive
    def bootstrap(self):
        if self.external.anchor is not None:raise Blocked('Already initialized')
        self._guard(); self.db.execute('BEGIN IMMEDIATE')
        try:
            state = self.v._verify()  # Full original replay/projection/integrity checks.
            n = self.db.execute('SELECT count(*) FROM journal').fetchone()[0]
            self.db.execute('CREATE TABLE incremental_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL)')
            value = self._envelope(state, n, len(state.accounting))
            self.db.execute('INSERT INTO incremental_state VALUES(1,?)', (encoded(value).decode(),))
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise
        self.external.publish(None, value)
        self.external.audit = dict(sequence=n, head=state.journal_head, at=self.clock())
        return deepcopy(value)

    def _load(self):
        rows = self.db.execute('SELECT payload FROM incremental_state').fetchall()
        if len(rows) != 1:raise Blocked('Missing/duplicate continuation')
        value = parse(rows[0][0].encode())
        if encoded(value).decode() != rows[0][0]:raise Blocked('Noncanonical state')
        if (value['reducer'], value['authorization'], value['policy']) != (
                self.reducer, self.authorization_id, self.policy):raise Blocked('State policy/identity drift')
        if value['schema'] != self._schema():raise Blocked('Schema/protection changed')
        tail = self.db.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
        if tail is None:
            # A verified genesis has no journal event yet. Do not confuse that
            # state with truncation of an already advanced compact checkpoint.
            identity=self.db.execute('SELECT * FROM identity').fetchall()
            if (type(value['sequence']) is not int or value['sequence']!=0
                    or value['state']['head']!=self.v.identity_id
                    or identity!=[(1,self.v.identity_id,encoded(self.v.identity).decode())]):
                raise Blocked('Journal tail mismatch')
            tail=(0,self.v.identity_id)
        if tail != (value['sequence'], value['state']['head']):raise Blocked('Journal tail mismatch')
        return value

    def _state(self, value):
        return restore(value['state'], self.v.binding, self.v.rules, self.v.identity_id)

    def _check_rows(self, rows):
        for table, expected in rows.items():
            columns = [r[1] for r in self.db.execute('PRAGMA table_info('+table+')')]
            for row in expected:
                where = ' AND '.join(c+'=?' for c in columns[:KEYS[table]])
                actual = self.db.execute('SELECT * FROM '+table+' WHERE '+where, row[:KEYS[table]]).fetchall()
                if actual != [tuple(row)]:raise Blocked('Projection mismatch: '+table)

    def _write_delta(self, before, after):
        for table, rows in after.items():
            old = {tuple(r[:KEYS[table]]): r for r in before.get(table, [])}
            for row in rows:
                key = tuple(row[:KEYS[table]])
                if key in old and tuple(old[key]) == tuple(row):continue
                columns = [r[1] for r in self.db.execute('PRAGMA table_info('+table+')')]
                where = ' AND '.join(c+'=?' for c in columns[:KEYS[table]])
                exists = self.db.execute('SELECT * FROM '+table+' WHERE '+where, key).fetchone()
                if exists is not None:
                    if table not in MUTABLE or key not in old or exists != tuple(old[key]):
                        raise Blocked('Conflicting existing financial row')
                    assignments = ','.join(c+'=?' for c in columns[KEYS[table]:])
                    self.db.execute('UPDATE '+table+' SET '+assignments+' WHERE '+where,
                                    tuple(row[KEYS[table]:])+key)
                else:
                    self.db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')', row)

    def _reduce(self, value, kind, data):
        s = self._state(value); before = s.rows()
        # Request and financial rules are the existing, version-bound rules.
        request = PaperLedger._request(s, kind, data)
        result = s.apply(kind, deepcopy(data))
        event = dict(label=LABEL, parent=s.journal_head, request=request, kind=kind, input=data, result=result)
        digest = fingerprint(event); s.journal_head = digest; s.event_ids[request] = digest
        after = s.rows()
        after['equity'] = [(r[0]+value['financial_count'], *r[1:]) for r in after['equity']]
        new = self._envelope(s, value['sequence']+1, value['financial_count']+len(s.accounting))
        return request, event, digest, before, after, new

    def _retry_request(self, state, kind, data):
        # Closed positions are absent from compact state. Their exact persisted
        # requests remain addressable for idempotent retry, never fresh execution.
        if kind == 'BAR' and data.get('candle') is not None:
            return 'BAR:'+data['owner']+':'+str(data['candle']['candle']['open_time_ms'])
        return PaperLedger._request(state,kind,data)

    def _recover(self):
        value = self._load(); previous = self.external.anchor
        if previous is None:raise Blocked('No independent continuity anchor')
        if value != previous:
            if value['sequence'] != previous['sequence']+1:raise Blocked('Unrecoverable continuity divergence')
            row = self.db.execute('SELECT digest,request,parent,payload FROM journal WHERE sequence=?',
                                  (value['sequence'],)).fetchone()
            event = parse(row[3].encode())
            req, expected, digest, _, after, new = self._reduce(previous,event['kind'],event['input'])
            if (event != expected or row[:3] != (digest,req,previous['state']['head']) or new != value):
                raise Blocked('Unpublished commit cannot be reproduced')
            self._check_rows(after)
        self._check_rows(self._state(value).rows())
        return previous, value

    @exclusive
    def recover(self):
        self._guard(); self.db.execute('BEGIN IMMEDIATE')
        try:
            previous, value = self._recover(); self.db.commit()
        except BaseException:
            self.db.rollback(); self.external.blocked = True; raise
        if value != previous:self.external.publish(previous,value)
        return deepcopy(value)

    @exclusive
    def apply(self, kind, data, *, fault=lambda point: None):
        self.recover()  # Publish any sole pending commit before another transition.
        self._guard(); self.db.execute('BEGIN IMMEDIATE')
        try:
            value = self._load()
            if value != self.external.anchor:raise Blocked('Continuity changed')
            self._audit_guard(value)
            self.evidence(kind,deepcopy(data))  # Synthetic trusted-source verifier, not a hash assertion.
            s = self._state(value); request = self._retry_request(s,kind,data)
            old = self.db.execute('SELECT payload,digest FROM journal WHERE request=?',(request,)).fetchone()
            if old:
                event = parse(old[0].encode())
                if (event['kind'] != kind or event['input'] != data or event['request'] != request
                        or fingerprint(event) != old[1]):raise Blocked('Conflicting retry')
                self.db.rollback(); return event['result']
            req,event,digest,before,after,new = self._reduce(value,kind,data)
            self._check_rows(before); fault('before_event')
            self.db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',
                            (new['sequence'],digest,req,event['parent'],encoded(event).decode()))
            fault('after_event'); self._write_delta(before,after); fault('after_financial')
            self.db.execute('UPDATE incremental_state SET payload=? WHERE singleton=1',(encoded(new).decode(),))
            fault('after_state'); self._check_rows(after)
            if self._load() != new:raise Blocked('Post-write state mismatch')
            self._guard(); self.db.commit()
        except BaseException:
            self.db.rollback(); raise
        fault('after_commit')
        self.external.publish(value,new); fault('after_publish')
        return event['result']

    def _audit_guard(self, value):
        audit = self.external.audit; now = self.clock()
        if (audit is None or now < audit['at'] or now-audit['at'] >= self.policy['max_age']
                or value['sequence']-audit['sequence'] >= self.policy['max_events']):
            raise Blocked('Historical audit expired')

    @exclusive
    def audit(self):
        """Independent full audit; never called by apply or a cache miss."""
        self._guard(); self.db.execute('BEGIN')
        try:
            state = self.v._verify(); value = self._load()
            n = self.db.execute('SELECT count(*) FROM journal').fetchone()[0]
            if value != self._envelope(state,n,len(state.accounting)) or value != self.external.anchor:
                raise Blocked('Historical audit divergence')
            self.db.rollback()
        except BaseException:
            self.db.rollback(); self.external.blocked = True; raise
        self.external.audit = dict(sequence=n,head=state.journal_head,at=self.clock())
        return deepcopy(self.external.audit)
