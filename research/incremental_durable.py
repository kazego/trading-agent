"""Windows synthetic-only adapter. No production loader or authorization.

Full continuity scan on reopen is intentional in this validation prototype.
Files are create-once; partial publications block instead of being repaired.
"""
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from research.incremental_ledger import SyntheticLedger, Blocked
from research.spec import encoded, fingerprint

AUTHORIZED = Path(r'C:\Users\diego\AppData\Local\TradingAgentSyntheticTests')


def audit_record(value):
    """Persist the exact existing clock sample, never a JSON float.

    Binary64 hexadecimal seconds round-trip without rounding or unit changes.
    Integer synthetic/legacy seconds retain their existing representation.
    Financial and continuity encoders remain strictly unchanged.
    """
    result=dict(value);at=result['at']
    if type(at) is float:
        if not math.isfinite(at) or at<0:raise Blocked('Invalid audit timestamp')
        result['at']={'encoding':'seconds-binary64-hex-v1','value':at.hex()}
    elif type(at) is not int or at<0:raise Blocked('Invalid audit timestamp')
    encoded(result)  # Reject floats in every other field, even nested ones.
    return result


def audit_value(record):
    result=dict(record);at=result['at']
    if type(at) is int and at>=0:return result
    if not isinstance(at,dict) or set(at)!={'encoding','value'} or at['encoding']!='seconds-binary64-hex-v1':
        raise Blocked('Invalid persisted audit timestamp')
    if type(at['value']) is not str:raise Blocked('Invalid persisted audit timestamp')
    try:sample=float.fromhex(at['value'])
    except (ValueError,OverflowError) as exc:raise Blocked('Invalid persisted audit timestamp') from exc
    if not math.isfinite(sample) or sample<0 or sample.hex()!=at['value']:
        raise Blocked('Noncanonical audit timestamp')
    result['at']=sample
    return result


def decode_record(raw, parent):
    record=json.loads(raw)
    if (set(record)!= {'parent','value','value_hash'} or encoded(record)!=raw
            or record['parent']!=parent or fingerprint(record['value'])!=record['value_hash']):
        raise Blocked('Continuity framing/hash')
    return record


def root_path(raw):
    p = Path(raw)
    if p != AUTHORIZED or p.resolve(strict=True) != p:
        raise Blocked('Exact authorized root required; no aliases')
    for q in (p, *p.parents):
        if q.is_symlink() or q.is_junction():
            raise Blocked('Reparse root forbidden')
    return p


def checked(path):
    p = Path(path)
    if p.resolve(strict=True) != p or p.is_symlink() or p.is_junction():
        raise Blocked('Aliased path')
    if p.is_file() and p.stat().st_nlink != 1:
        raise Blocked('Hardlink forbidden')
    return p


def create(path, data):
    with open(path, 'xb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    if checked(path).read_bytes() != data:
        raise Blocked('Readback mismatch')


def new_run(raw):
    root = root_path(raw)
    p = root / ('incremental-' + uuid.uuid4().hex)
    p.mkdir(exist_ok=False); checked(p)
    create(p/'marker.json', encoded({'kind':'synthetic-durable-v1','run':p.name}))
    return p


class Ownership:
    """Lifetime Windows byte-range lock, including SQLite open/close/recovery."""
    def __init__(self, run):
        if os.name != 'nt':raise Blocked('Windows runner required')
        root_path(run.parent); checked(run)
        if not run.name.startswith('incremental-'):raise Blocked('Wrong run')
        marker=json.loads(checked(run/'marker.json').read_bytes())
        if marker != {'kind':'synthetic-durable-v1','run':run.name}:raise Blocked('Marker mismatch')
        path=run/'ownership.lock'
        if not path.exists():
            try:create(path,b'0')
            except FileExistsError:pass
        checked(path)
        self.file=open(path,'r+b');self.active=False
        import msvcrt
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.file.close();raise Blocked('Writer already owns run')
        self.active=True;self.run=run

    def close(self):
        if self.active:
            import msvcrt
            self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            self.active=False;self.file.close()


class DurableExternal:
    def __init__(self, owner, fault=lambda point:None):
        self.owner=owner;self.lock=threading.RLock();self.fault=fault

    def _owned(self):
        if not self.owner.active:raise Blocked('Ownership lost')

    def _chain(self, prefix):
        self._owned();root=self.owner.run
        files=sorted(root.glob(prefix+'-*.json')); receipts=sorted(root.glob(prefix+'-*.receipt'))
        if len(files)!=len(receipts):raise Blocked('Partial publication')
        result=[];previous=None
        for i,(path,receipt) in enumerate(zip(files,receipts)):
            stem=f'{prefix}-{i:012d}'
            if path.name!=stem+'.json' or receipt.name!=stem+'.receipt':raise Blocked('Missing record')
            raw=checked(path).read_bytes();record=decode_record(raw,previous)
            digest=fingerprint(record)
            if checked(receipt).read_bytes()!=digest.encode():raise Blocked('Continuity checksum')
            result.append(record['value']);previous=digest
        return result,previous

    def _append(self,prefix,value):
        records,parent=self._chain(prefix);stem=f'{prefix}-{len(records):012d}'
        raw=encoded({'parent':parent,'value':value,'value_hash':fingerprint(value)})
        path=self.owner.run/(stem+'.json')
        with open(path,'xb') as f:
            f.write(raw[:len(raw)//2]);f.flush();self.fault('partial_record')
            f.write(raw[len(raw)//2:]);f.flush();os.fsync(f.fileno())
        self.fault('record_synced')
        create(self.owner.run/(stem+'.receipt'),fingerprint(json.loads(raw)).encode())
        self.fault('receipt_synced')
        if self._chain(prefix)[0][-1]!=value:raise Blocked('Publication readback')

    @property
    def anchor(self):
        values,_=self._chain('continuity')
        for a,b in zip(values,values[1:]):
            if b['sequence']!=a['sequence']+1:raise Blocked('Sequence mismatch')
        return values[-1] if values else None

    def publish(self,previous,current):
        if self.blocked or self.anchor!=previous:raise Blocked('Continuity CAS')
        if previous is not None and current['sequence']!=previous['sequence']+1:raise Blocked('Sequence mismatch')
        # Full envelope includes sequence, journal head, identities and state.
        self._append('continuity',current)

    @property
    def audit(self):
        values,_=self._chain('audit')
        return audit_value(values[-1]) if values else None

    @audit.setter
    def audit(self,value):self._append('audit',audit_record(value))

    @property
    def blocked(self):
        self._owned();return (self.owner.run/'BLOCKED').exists()

    @blocked.setter
    def blocked(self,value):
        if not value:raise Blocked('Cannot clear latch')
        if not self.blocked:create(self.owner.run/'BLOCKED',b'Blocked; preserve evidence\n')


class DurableLedger(SyntheticLedger):
    def database_path(self):
        return self.external.owner.run/'synthetic.sqlite'

    def _recover(self):
        value=self._load();previous=self.external.anchor
        if previous is not None and value!=previous and value['sequence']==previous['sequence']+1:
            row=self.db.execute('SELECT payload FROM journal WHERE sequence=?',(value['sequence'],)).fetchone()
            if row is None:raise Blocked('Missing pending event')
            event=json.loads(row[0])
            self.evidence(event['kind'],event['input'])
        return super()._recover()

    def _validate_database(self):
        owner=self.external.owner;owner.active or self._lost()
        rows=self.db.execute('PRAGMA database_list').fetchall()
        expected=self.database_path()
        if not expected.is_relative_to(owner.run):
            raise Blocked('Database outside owned synthetic run')
        main=[r for r in rows if r[1]=='main']
        # integrity_check may initialize SQLite's anonymous, empty temp schema.
        # This is not an attached fixture or permission to use temporary objects.
        others=[r for r in rows if r[1]!='main']
        if (len(main)!=1 or checked(Path(main[0][2]))!=expected
                or any(r[1]!='temp' or r[2] for r in others)
                or len(others)>1):
            raise Blocked('Only owned synthetic database permitted')
        if others and self.db.execute('SELECT name FROM temp.sqlite_master').fetchall():
            raise Blocked('Temporary database objects forbidden')
        if self.db.execute('PRAGMA synchronous').fetchone()[0]!=2:raise Blocked('FULL required')

    def _lost(self):raise Blocked('Ownership lost')


def connect(owner, *, initialize=False):
    if not owner.active:raise Blocked('Ownership lost')
    path=owner.run/'synthetic.sqlite'
    if initialize:create(path,b'')
    checked(path)
    db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=0)
    db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    return db
