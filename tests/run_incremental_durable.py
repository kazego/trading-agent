"""Explicit manual Windows runner. No operational inputs; no automatic cleanup."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

REPO=Path(__file__).resolve(strict=True).parents[1]
sys.path.insert(0,str(REPO))
sys.path.insert(0,str(REPO/'tests'))
from research.incremental_durable import (root_path,new_run,Ownership,DurableExternal,
                                        DurableLedger,connect,create,checked)
from research.incremental_ledger import Blocked
from research.spec import encoded
from durable_runner_security import Policy, Registry, validate_grant, NAME, invoke, GLOBAL_SECONDS, encode_telemetry, report_preserving


def guard(cases):
    libraries=(Path(sys.base_prefix)/'Lib',Path(sys.prefix)/'Lib',Path(sys.base_prefix)/'DLLs',
               Path(sys.base_prefix)/f'python{sys.version_info.major}{sys.version_info.minor}.zip')
    policy=Policy(cases,REPO,libraries)
    sys.addaudithook(policy)
    return policy


def worker(run,action,point):
    owner=Ownership(run)  # Fail before constructing/opening anything if occupied.
    db=None;f=None
    try:
        from test_transactional_bar import fixture
        f,v,rid,stamp,candles=fixture(bars=2,backlog=4)
        memory=v._db;db=connect(owner,initialize=action=='init')
        if action=='init':memory.backup(db)
        v._db=db
        def fault(stage):
            if stage==point:os._exit(77)  # Synthetic process only; no graceful rollback.
        external=DurableExternal(owner,fault)
        def evidence(kind,data):
            if kind!='BAR' or data['candle']!=candles['BTCUSDT',data['candle']['candle']['open_time_ms']].record:
                raise Blocked('Synthetic source mismatch')
        core=DurableLedger(v,external,authorization_id='synthetic-durable',
            authorize=lambda:'synthetic-durable',evidence=evidence)
        data=dict(owner=rid,candle=candles['BTCUSDT',stamp].record)
        if action=='init':core.bootstrap()
        elif action in ('apply','apply-next'):
            if action=='apply-next':data['candle']=candles['BTCUSDT',stamp+900000].record
            core.apply('BAR',data,fault=fault)
        elif action=='recover':core.recover()
        elif action=='corrupt':
            db.execute("UPDATE account SET amount='7'");db.commit()
        elif action=='restore-old':
            image=checked(run/'old.sqlite.bytes').read_bytes()
            # Synthetic fault injection only. Restore through SQLite, not live file replacement.
            import sqlite3
            old=sqlite3.connect(':memory:');old.deserialize(image);old.backup(db);old.close()
        elif action=='corrupt-continuity':
            path=sorted(run.glob('continuity-*.json'))[-1]
            with open(checked(path),'ab') as out:
                out.write(b'corrupt');out.flush();os.fsync(out.fileno())
        else:raise ValueError('Unknown action')
        if action not in ('corrupt','restore-old','corrupt-continuity'):
            core.audit()
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise Blocked('SQLite integrity')
        print(json.dumps({'action':action,'sequence':db.execute('SELECT count(*) FROM journal').fetchone()[0],
            'head':db.execute('SELECT digest FROM journal ORDER BY sequence DESC LIMIT 1').fetchone()[0],
            'equity':db.execute('SELECT amount FROM account').fetchone()[0]}),flush=True)
    finally:
        if db is not None:db.close()
        if f is not None:f.doCleanups()
        owner.close()


def matrix(root):
    deadline=time.monotonic()+GLOBAL_SECONDS
    suite=new_run(str(root));results=[];counter=0;failure=None
    policy=guard((suite,));registry=Registry(suite)
    def allocate():
        if time.monotonic()>=deadline:raise TimeoutError('Global limit before allocation')
        path=root/('incremental-'+uuid.uuid4().hex)
        policy.mkdir_target=path
        try:path.mkdir(exist_ok=False)
        finally:policy.mkdir_target=None
        checked(path);policy.data_roots.add(path)
        create(path/'marker.json',encoded({'kind':'synthetic-durable-v1','run':path.name}))
        binding=registry.register_new(path)
        create(path/'case-binding.json',encoded(binding))
        return path
    def call(run,action,point='',expect=0):
        nonlocal counter
        counter+=1
        grant=registry.grant(run,action,point)
        command=[sys.executable,'-I','-B',str(Path(__file__).resolve(strict=True)),
                 '--root',str(root),'--worker']
        return invoke(command,grant,policy,deadline,
            lambda record:create(suite/f'worker-{counter:04d}.json',encode_telemetry(record)),expect=expect)
    def fresh():
        r=allocate();initial=call(r,'init');return r,initial
    def record(name):
        results.append(name);print('PASS '+name,flush=True)
    try:
        r,start=fresh();end=call(r,'apply');retry=call(r,'apply')
        assert end==retry and end['sequence']==start['sequence']+1
        record('commit_reopen_identical_retry')
        for stage in ('before_event','after_event','after_financial','after_state','after_commit','after_publish'):
            r,start=fresh();call(r,'apply',stage,77);recovered=call(r,'recover')
            assert recovered['sequence']==start['sequence']+(stage in ('after_commit','after_publish'))
            done=call(r,'apply');assert done['sequence']==start['sequence']+1
            record('process_exit_'+stage)
        for stage in ('partial_record','record_synced','receipt_synced'):
            r,start=fresh();call(r,'apply',stage,77)
            recovered=call(r,'recover',expect=0 if stage=='receipt_synced' else 23)
            if recovered:assert recovered['sequence']==start['sequence']+1
            record('publication_'+stage)
        r,start=fresh();call(r,'apply','after_commit',77)
        call(r,'recover','partial_record',77);call(r,'recover',expect=23)
        record('recovery_interrupted_partial_publication')
        r,start=fresh();call(r,'corrupt');call(r,'recover',expect=23);call(r,'apply',expect=23)
        record('financial_corruption_latched')
        r,start=fresh();create(r/'old.sqlite.bytes',checked(r/'synthetic.sqlite').read_bytes())
        call(r,'apply');call(r,'restore-old');call(r,'recover',expect=23)
        record('old_database_against_new_anchor')
        r,start=fresh();call(r,'corrupt-continuity');call(r,'recover',expect=23)
        record('corrupt_external_record')
        r,start=fresh();call(r,'apply');call(r,'apply-next')
        clone=allocate()
        # Fault fixture: new DB paired with an old independently captured prefix.
        # Originals are never deleted/replaced. Copy only synthetic known files.
        for name in ('synthetic.sqlite','continuity-000000000000.json',
                     'continuity-000000000000.receipt','audit-000000000000.json',
                     'audit-000000000000.receipt'):
            create(clone/name,checked(r/name).read_bytes())
        call(clone,'recover',expect=23);record('old_external_prefix_two_events_behind')
        r,start=fresh();owner=Ownership(r)
        try:call(r,'apply',expect=23)
        finally:owner.close()
        call(r,'apply');record('cross_process_exclusion_and_release')
        if time.monotonic()>deadline:raise TimeoutError('Global limit before completion')
        return {'status':'PASS','cases':results,'evidence':str(suite)}
    except BaseException as exc:
        failure=exc
        report_preserving(exc,lambda:create(suite/'FAIL.json',encoded({
            'status':'FAIL','error':type(exc).__name__+': '+str(exc),
            'notes':list(getattr(exc,'__notes__',()))})), 'FAIL.json')
        raise
    finally:
        report_preserving(failure,lambda:create(suite/'completed-cases.json',encoded(results)),
                          'Completed-cases report')
        report_preserving(failure,lambda:print('Preserved evidence: '+str(suite),flush=True),
                          'Evidence location output')


def main():
    parser=argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--root',required=True)
    parser.add_argument('--worker',action='store_true')
    args=parser.parse_args();root=root_path(args.root)
    if args.worker:
        # Only the current parent knows the ephemeral case capability. No path,
        # action or capability is accepted on argv or persisted in logs.
        raw=sys.stdin.read(4097)
        if len(raw)>4096:raise PermissionError('Oversized worker grant')
        grant=json.loads(raw)
        if not isinstance(grant,dict) or not isinstance(grant.get('case'),str) or not NAME.fullmatch(grant['case']):
            raise PermissionError('Invalid case')
        run=checked(root/grant['case'])
        # Only this fixed binding metadata is read before membership verification.
        binding=json.loads(checked(run/'case-binding.json').read_bytes())
        validate_grant(grant,binding)
        guard((run,))
        worker(run,grant['action'],grant['fault'])
    else:print(json.dumps(matrix(root)),flush=True)


if __name__=='__main__':
    try:main()
    except (Blocked,ValueError,KeyError,PermissionError,json.JSONDecodeError) as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        for note in getattr(exc,'__notes__',()):print(note,file=sys.stderr)
        sys.exit(23)
