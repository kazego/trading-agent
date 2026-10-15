"""Experimental capture primitive; no operational paths or certificate authority.

Caller owns a synthetic read-only WAL connection and supplies the same lock
used by its synthetic writer. Destination is a fresh in-memory SQLite snapshot.
Disk sealing detects interrupted/altered captures; it grants no audit authority.
Auditor isolation and certificate publication are NOT implemented.
"""
import json
import sqlite3
import time
import hashlib
import os
from pathlib import Path
import uuid
import re

from research.spec import encoded, fingerprint


class CaptureRejected(RuntimeError):
    pass


ROOT = Path(r'C:\Users\diego\AppData\Local\TradingAgentSyntheticTests')


def synthetic_path(path):
    path = Path(path)
    if not path.is_relative_to(ROOT) or path.resolve(strict=True) != path:
        raise CaptureRejected('Outside synthetic root or aliased path')
    for p in (path, *path.parents):
        if p.is_symlink() or p.is_junction():
            raise CaptureRejected('Reparse path rejected')
    if path.is_file() and path.stat().st_nlink != 1:
        raise CaptureRejected('Hardlink rejected')
    return path


def file_hash(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def create_record(path, value):
    raw = encoded(value)
    with open(path, 'xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise CaptureRejected('Record readback mismatch')


def read_record(path):
    raw = synthetic_path(path).read_bytes()
    value = json.loads(raw)
    if encoded(value) != raw:
        raise CaptureRejected('Noncanonical record')
    return value


def validate_manifest(manifest, seal, anchor, expected_id):
    fields = {'version','snapshot_id','sequence','head','state_hash','file_sha256',
              'file_size','captured_at_ns','anchor'}
    if not isinstance(manifest,dict) or set(manifest) != fields or manifest['version'] != 'synthetic-snapshot-v1':
        raise CaptureRejected('Manifest schema')
    if not isinstance(manifest['file_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',manifest['file_sha256']):
        raise CaptureRejected('Invalid file digest')
    if seal != {'version':'snapshot-seal-v1','manifest_hash':fingerprint(manifest)}:
        raise CaptureRejected('Seal mismatch')
    if (manifest['snapshot_id'] != expected_id or manifest['anchor'] != anchor
            or manifest['sequence'] != anchor['sequence']
            or manifest['head'] != anchor['state']['head']
            or manifest['state_hash'] != fingerprint(anchor)):
        raise CaptureRejected('Manifest/independent anchor mismatch')
    for name in ('sequence','file_size','captured_at_ns'):
        if type(manifest[name]) is not int or manifest[name] < 0:
            raise CaptureRejected('Invalid integer metadata')


def verify_sqlite(path, expected_anchor):
    db = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or state(db) != expected_anchor:
            raise CaptureRejected('SQLite/anchor mismatch')
    finally:
        db.close()


def verify_sealed(directory, expected_anchor):
    """Read-only verification against caller's independently verified anchor.

    Hashes alongside files alone do not authorize or establish provenance.
    No concurrent malicious writer protection is claimed by this interface.
    """
    directory = synthetic_path(directory)
    if {p.name for p in directory.iterdir()} != {'snapshot.sqlite','manifest.json','seal.json'}:
        raise CaptureRejected('Incomplete snapshot or unexpected sidecar')
    manifest = read_record(directory/'manifest.json')
    seal = read_record(directory/'seal.json')
    validate_manifest(manifest, seal, expected_anchor, directory.name)
    path = synthetic_path(directory/'snapshot.sqlite')
    before = file_hash(path)
    if before != manifest['file_sha256'] or path.stat().st_size != manifest['file_size']:
        raise CaptureRejected('Snapshot file changed')
    verify_sqlite(path,expected_anchor)
    if file_hash(path) != before:
        raise CaptureRejected('Snapshot changed during verification')
    return manifest


def capture_sealed(reader, ownership, read_anchor, parent, *, after_pin=lambda:None,
                   fault=lambda stage:None):
    """Create-only snapshot; preserve incomplete output on failure, never repair.

    SQLite closes before fsync/hash; manifest and final seal use flush/fsync.
    This does not claim directory-entry or hardware power-loss durability.
    """
    parent = synthetic_path(parent)
    directory = parent/('sealed-'+uuid.uuid4().hex)
    directory.mkdir(exist_ok=False)
    synthetic_path(directory)
    snapshot = None
    try:
        snapshot, metrics = capture(reader, ownership, read_anchor, after_pin=after_pin)
        anchor = state(snapshot)
        fault('after_capture')
        path = directory/'snapshot.sqlite'
        with path.open('xb'):
            pass
        disk = sqlite3.connect(path)
        start = time.perf_counter_ns()
        try:
            snapshot.backup(disk)
            disk.execute('PRAGMA journal_mode=DELETE')
        finally:
            disk.close()
        with path.open('r+b') as stream:
            stream.flush(); os.fsync(stream.fileno())
        metrics['disk_copy_flush_ns'] = time.perf_counter_ns()-start
        fault('after_sqlite')
        if {p.name for p in directory.iterdir()} != {'snapshot.sqlite'}:
            raise CaptureRejected('Unexpected SQLite sidecar')
        verify_sqlite(synthetic_path(path),anchor)  # Never seal an unverified disk copy.
        manifest = dict(version='synthetic-snapshot-v1',snapshot_id=directory.name,
                        sequence=anchor['sequence'],head=anchor['state']['head'],
                        state_hash=fingerprint(anchor),file_sha256=file_hash(path),
                        file_size=path.stat().st_size,captured_at_ns=metrics['captured_at_ns'],anchor=anchor)
        create_record(directory/'manifest.json',manifest)
        fault('after_manifest')
        create_record(directory/'seal.json',dict(version='snapshot-seal-v1',manifest_hash=fingerprint(manifest)))
        fault('after_seal')
        verify_sealed(directory,anchor)
        return directory, manifest, metrics
    finally:
        if snapshot is not None:snapshot.close()


def state(reader):
    rows = reader.execute('SELECT payload FROM incremental_state').fetchall()
    if len(rows) != 1:
        raise CaptureRejected('Missing/duplicate compact state')
    value = json.loads(rows[0][0])
    if encoded(value).decode() != rows[0][0]:
        raise CaptureRejected('Noncanonical compact state')
    tail = reader.execute('SELECT sequence,digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()
    if tail != (value['sequence'], value['state']['head']):
        raise CaptureRejected('Tail/state disagreement')
    return value


def capture(reader, ownership, read_anchor, *, after_pin=lambda: None, timeout_s=30):
    """Pin under ownership, release ownership, then copy that exact read view.

The synthetic marker is a scope assertion, not an authorization credential.
read_anchor must return an independently verified anchor under ownership.
No historical replay is performed or claimed here.
"""
    if reader.in_transaction:
        raise CaptureRejected('Caller transaction forbidden')
    if reader.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise CaptureRejected('Read-only connection required')
    if reader.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
        raise CaptureRejected('WAL required')
    if reader.execute('SELECT kind FROM snapshot_fixture').fetchall() != [('synthetic-snapshot-v1',)]:
        raise CaptureRejected('Synthetic fixture required')
    target = sqlite3.connect(':memory:')
    started = time.perf_counter_ns()
    try:
        with ownership:
            acquired = time.perf_counter_ns()
            reader.execute('BEGIN')
            captured = state(reader)  # First actual read fixes the WAL snapshot.
            anchor = json.loads(encoded(read_anchor()))
            if captured != anchor:
                raise CaptureRejected('Anchor/state disagreement')
            captured_at_ns = time.time_ns()
        released = time.perf_counter_ns()
        after_pin()  # Test barrier: concurrent writer must COMMIT before backup.
        copy_start = time.perf_counter_ns()
        def progress(status, remaining, total):
            if time.perf_counter_ns() - started > timeout_s * 1_000_000_000:
                raise CaptureRejected('Capture timeout')
        reader.backup(target, pages=16, progress=progress, sleep=0.01)
        copy_end = time.perf_counter_ns()
        if state(target) != captured or target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise CaptureRejected('Copied snapshot mismatch')
        target.execute('PRAGMA query_only=ON')
        return target, dict(sequence=captured['sequence'], head=captured['state']['head'],
                            state_hash=fingerprint(captured), captured_at_ns=captured_at_ns,
                            ownership_wait_ns=acquired-started, ownership_held_ns=released-acquired,
                            copy_ns=copy_end-copy_start)
    except BaseException:
        target.close()
        raise
    finally:
        if reader.in_transaction:
            reader.rollback()
