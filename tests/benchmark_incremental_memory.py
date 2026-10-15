"""Synthetic in-memory benchmark; no production consumer or filesystem fixtures.

Run with -B. JSONL stdout is the report. Preparation and complete audits are
outside the timed samples. All financial replay/identity checks remain enabled.
Samples recreate the core object, not the Python process, and advance consecutive
bars (N..N+29); they are not cold-process or durable-commit measurements.
"""
import argparse
from collections import defaultdict
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from research.incremental_ledger import SyntheticLedger, ExternalMemory
from research.spec import encoded
from paper.journal import Replay
from test_transactional_bar import fixture


def isolation(event, args):
    if event == 'sqlite3.connect' and args[0] != ':memory:':
        raise PermissionError('Only synthetic :memory: SQLite authorized')
    if event.startswith(('socket.', 'urllib.', 'subprocess.', 'os.exec', 'os.spawn')) or event in (
            'os.system', 'os.startfile', 'os.posix_spawn'):
        raise PermissionError('No network or child process in memory benchmark')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
        if 'artifacts' in Path(path).parts:
            raise PermissionError('No artifact/evidence reads')
        mode, flags = args[1:3]
        if ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            raise PermissionError('No filesystem writes in memory benchmark')
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink', 'os.chmod'):
        raise PermissionError('No filesystem mutations')


class Meter:
    def __init__(self):
        self.stack=[]; self.rows=defaultdict(lambda:dict(calls=0,inclusive_s=0.,exclusive_s=0.))

    @contextmanager
    def span(self, name):
        frame=[time.perf_counter(),0.]; self.stack.append(frame)
        try:yield
        finally:
            elapsed=time.perf_counter()-frame[0];self.stack.pop()
            row=self.rows[name];row['calls']+=1;row['inclusive_s']+=elapsed;row['exclusive_s']+=elapsed-frame[1]
            if self.stack:self.stack[-1][1]+=elapsed

    def wrap(self, obj, name, label):
        original=getattr(obj,name)
        def call(*args,**kwargs):
            with self.span(label):return original(*args,**kwargs)
        setattr(obj,name,call)


class TimedConnection:
    """Delegates to the same SQLite connection; does not change transactions."""
    def __init__(self, db, meter):self.db=db;self.meter=meter
    def __getattr__(self, name):return getattr(self.db,name)
    def commit(self):
        with self.meter.span('sqlite_commit'):return self.db.commit()
    def rollback(self):
        with self.meter.span('sqlite_rollback'):return self.db.rollback()


def stats(values):
    ordered=sorted(values)
    return dict(n=len(values),median=statistics.median(ordered),
                p95=ordered[math.ceil(.95*len(ordered))-1],maximum=max(ordered))


def run_size(size, samples=30, emit=lambda record:print(json.dumps(record),flush=True)):
    if size<69 or samples<1 or samples>222:raise ValueError('Unsupported fixture/sample count')
    phase='prepare';calls=0;started=time.perf_counter();original=Replay.apply
    def replay(self,*args,**kwargs):
        nonlocal calls
        result=original(self,*args,**kwargs);calls+=1
        if calls%1000==0:emit(dict(type='progress',size=size,phase=phase,reducer_calls=calls,elapsed_s=time.perf_counter()-started))
        return result
    with patch.object(Replay,'apply',replay):
        f,v,owner,stamp,candles=fixture(bars=size-68,accepts=66,backlog=222)
        preparation=time.perf_counter()-started
        db=v._db;external=ExternalMemory()
        def evidence(kind,data):
            if kind!='BAR':raise ValueError('Only BAR workload in this benchmark')
            c=data['candle']['candle']
            if candles[c['symbol'],c['open_time_ms']].record!=data['candle']:raise ValueError('Synthetic source mismatch')
        def construct():
            return SyntheticLedger(v,external,authorization_id='synthetic-memory-benchmark',
                authorize=lambda:'synthetic-memory-benchmark',evidence=evidence,clock=lambda:100)
        try:
            phase='bootstrap_full';t=time.perf_counter();initial=construct().bootstrap();bootstrap=time.perf_counter()-t
            assert initial['sequence']==size
            sizes=[len(encoded(initial))];measurements=[]
            emit(dict(type='prepared',size=size,preparation_s=preparation,bootstrap_full_s=bootstrap,
                      history=dict(accept=66,confirm=1,entry=1,bar=size-68),compact_bytes=sizes[0]))
            phase='samples'
            for i in range(samples):
                meter=Meter();v._db=TimedConnection(db,meter)
                # Binding wrappers is outside the measured financial work, but
                # initialization of the core is explicitly included below.
                old_publish=external.publish
                meter.wrap(external,'publish','continuity_memory_publication')
                t=time.perf_counter()
                with meter.span('core_initialization'):core=construct()
                for method,label in (
                    ('_guard','authorization_identity_checks'),('_load','state_tail_schema_validation'),
                    ('_state','restore_compact'),('_check_rows','projection_validation'),
                    ('_reduce','financial_reducer'),('_write_delta','projection_delta'),
                    ('_recover','local_recovery_validation')):
                    meter.wrap(core,method,label)
                data=dict(owner=owner,candle=candles['BTCUSDT',stamp+i*900000].record)
                with meter.span('apply_total'):result=core.apply('BAR',data)
                total=time.perf_counter()-t
                external.publish=old_publish;v._db=db
                assert result['status']=='OPEN'
                value=external.anchor;sizes.append(len(encoded(value)))
                assert value['sequence']==size+i+1 and value['state']['equity']=='1000'
                record=dict(type='sample',size=size,sample=i+1,total_s=total,stages=dict(meter.rows),compact_bytes=sizes[-1])
                measurements.append(record);emit(record)
            phase='audit_full';t=time.perf_counter();construct().audit();audit=time.perf_counter()-t
            final=construct().recover()
            assert final['sequence']==size+samples and final['state']['slot'][0]=='OPEN'
            summary=dict(type='summary',size=size,total=stats([r['total_s'] for r in measurements]),
                stage_inclusive={k:stats([r['stages'].get(k,{}).get('inclusive_s',0) for r in measurements])
                                 for k in measurements[0]['stages']},
                preparation_s=preparation,bootstrap_full_s=bootstrap,audit_full_s=audit,
                compact_bytes_min=min(sizes),compact_bytes_max=max(sizes),
                financial=dict(equity=final['state']['equity'],slot=final['state']['slot'][0],
                               journal_count=final['sequence'],advanced_bars=samples,audit='PASS'))
            emit(summary);return summary
        finally:
            v._db=db;f.doCleanups()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sizes',type=int,nargs='+',default=[348,700,7000,70000])
    parser.add_argument('--samples',type=int,default=30);args=parser.parse_args()
    if args.samples<30:parser.error('At least 30 samples required for reported matrix')
    sys.dont_write_bytecode=True;sys.addaudithook(isolation)
    print(json.dumps(dict(type='scope',mode='MEMORY_CORE_ONLY',cold_python=False,durable=False,
        source='synthetic dictionary equality, not LIVE-0A verification',checks='unmodified financial/replay rules')),
        flush=True)
    summaries=[run_size(n,args.samples) for n in args.sizes]
    print(json.dumps(dict(type='matrix_complete',summaries=summaries)),flush=True)
