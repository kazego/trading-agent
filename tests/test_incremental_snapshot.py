"""Synthetic WAL experiment with a distinct writer process; no live imports."""
import json
from contextlib import nullcontext
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import unittest
import uuid

# Permit direct execution with -I, including multiprocessing spawn imports.
sys.path.insert(0, str(Path(__file__).resolve(strict=True).parents[1]))

from research.incremental_snapshot import capture, CaptureRejected
from research.spec import encoded, fingerprint

AUTHORIZED_ROOT = Path(r'C:\Users\diego\AppData\Local\TradingAgentSyntheticTests')


def new_directory():
    root = AUTHORIZED_ROOT.resolve(strict=True)
    if root != AUTHORIZED_ROOT or any(
            p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise PermissionError('Synthetic root must not be aliased')
    directory = root / ('snapshot-' + uuid.uuid4().hex)
    directory.mkdir(exist_ok=False)
    if directory.resolve(strict=True) != directory:
        raise PermissionError('Aliased synthetic directory')
    print('SYNTHETIC_DIRECTORY', str(directory), flush=True)
    return directory


def envelope(n):
    return dict(sequence=n, state=dict(head=fingerprint({'synthetic': n}), equity='1000'))


def append(db, n):
    value = envelope(n)
    db.execute('INSERT INTO journal VALUES (?,?)', (n, value['state']['head']))
    db.execute('UPDATE incremental_state SET payload=?', (encoded(value).decode(),))


def writer(path, lock, ready, proceed, result):
    try:
        db = sqlite3.connect(path, timeout=3)
        ready.set()
        if not proceed.wait(10):
            raise RuntimeError('Writer barrier timeout')
        with lock:
            db.execute('BEGIN IMMEDIATE')
            append(db, 2)
            db.commit()
        db.close()
        result.send('committed')
    except BaseException as exc:
        result.send(repr(exc))
    finally:
        result.close()


class SnapshotTests(unittest.TestCase):
    def test_concurrent_commit_does_not_enter_pinned_backup(self):
        # Strict resolution remains mandatory; a sandbox failure is not a PASS.
        # Preserve evidence; no recursive cleanup or permission adjustments.
        with nullcontext(new_directory()) as root:
            directory = Path(root).resolve(strict=True)
            path = directory / 'synthetic.sqlite'
            with path.open('xb'):
                pass
            db = sqlite3.connect(path)
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            db.executescript('CREATE TABLE snapshot_fixture(kind TEXT);'
                             "INSERT INTO snapshot_fixture VALUES ('synthetic-snapshot-v1');"
                             'CREATE TABLE journal(sequence INTEGER PRIMARY KEY,digest TEXT);'
                             'CREATE TABLE incremental_state(payload TEXT);'
                             "INSERT INTO incremental_state VALUES ('{}');")
            append(db, 1); db.commit()
            reader = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
            reader.execute('PRAGMA query_only=ON')
            ctx = multiprocessing.get_context('spawn')
            lock=ctx.Lock(); ready=ctx.Event(); proceed=ctx.Event()
            receive, send=ctx.Pipe(duplex=False)
            child=ctx.Process(target=writer,args=(str(path),lock,ready,proceed,send))
            snapshot=None
            try:
                child.start(); send.close()
                self.assertTrue(ready.wait(10))
                def concurrent_commit():
                    proceed.set()
                    self.assertTrue(receive.poll(10), 'Writer blocked by pinned reader')
                    self.assertEqual(receive.recv(), 'committed')
                    # Independent connection sees N+1 before backup is invoked.
                    self.assertEqual(db.execute('SELECT max(sequence) FROM journal').fetchone()[0],2)
                snapshot, metrics=capture(reader,lock,lambda:envelope(1),after_pin=concurrent_commit)
                self.assertEqual(db.execute('SELECT max(sequence) FROM journal').fetchone()[0],2)
                self.assertEqual(snapshot.execute('SELECT max(sequence) FROM journal').fetchone()[0],1,
                                 'Backup must retain N=1; including N+1 is a consistency failure')
                self.assertEqual(snapshot.execute('SELECT sequence,digest FROM journal ORDER BY sequence').fetchall(),
                                 [(1,envelope(1)['state']['head'])])
                self.assertEqual(json.loads(snapshot.execute('SELECT payload FROM incremental_state').fetchone()[0]),envelope(1))
                self.assertFalse(reader.in_transaction)
                child.join(3)
                self.assertFalse(child.is_alive(), 'Synthetic writer did not exit')
                self.assertEqual(child.exitcode,0)
                print('SYNTHETIC_CAPTURE_METRICS',json.dumps(metrics,sort_keys=True))
                with self.assertRaisesRegex(CaptureRejected,'Anchor/state'):
                    capture(reader,lock,lambda:envelope(1))
                self.assertFalse(reader.in_transaction)
                with self.assertRaisesRegex(CaptureRejected,'Capture timeout'):
                    capture(reader,lock,lambda:envelope(2),timeout_s=0)
                self.assertFalse(reader.in_transaction)
                with (directory/'capture-result.json').open('x', encoding='utf-8') as report:
                    json.dump(dict(status='PASS',original_sequence=2,snapshot_sequence=1,
                                   metrics_ns=metrics),report,sort_keys=True)
            finally:
                if child.pid is not None:
                    child.join(3)
                    if child.is_alive():child.terminate();child.join(3)
                receive.close();send.close()
                if snapshot is not None:snapshot.close()
                reader.close();db.close()


if __name__=='__main__':unittest.main()
