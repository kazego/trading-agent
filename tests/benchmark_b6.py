"""Explicit offline benchmark, synthetic stores only; not unittest discovery."""
import json
import sys
import os
import math
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from b6_fixture import SourceFixture
from forward.store import Store
from paper.operations import propose,paths,exclusive
from paper.consumer import consume,source_progress
from paper.source import SourceAdapter
import test_paper_settlement as b3
from research.spec import fingerprint,encoded
import sqlite3
from paper.transition import OLD_B4,OLD_B5,plan
from paper.acceptance import AcceptanceBinding,ProspectiveGate
from paper.model import DraftManifest,LABEL
from paper.journal import HISTORICAL,Replay
from paper.ledger import SCHEMA,IMMUTABLE


def measure(call):
    process=subprocess.run; verify=Store.verify
    start=time.perf_counter()
    with patch('paper.git_batch.subprocess.run',wraps=process) as git,patch.object(Store,'verify',autospec=True,side_effect=verify) as scans:
        result=call()
    return dict(seconds=time.perf_counter()-start,git_processes=git.call_count,full_source_passes=scans.call_count,result=result)


def fresh_process(root, run, now):
    code = """
import json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from forward import operations as fo
from paper.consumer import consume
sys.path.insert(0,'tests')
from benchmark_b6 import measure
root=Path(sys.argv[1]);run=sys.argv[2];now=int(sys.argv[3])
assert root.parent==Path(tempfile.gettempdir()) and root.name.startswith('b6-offline-')
source_root=json.loads((root/'artifacts/forward-paper/live-0a-001/operations-v1.json').read_bytes())
with patch.object(fo,'PATCH_ROOT',source_root['operations_fingerprint']),patch('paper.transition.time.time_ns',return_value=now*1000000):
    print(json.dumps(measure(lambda:consume(run,root=root,clock=lambda:now))))
"""
    start=time.perf_counter()
    process=subprocess.run([sys.executable,'-B','-c',code,str(root),run,str(now)],
        capture_output=True,check=True,timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    total=time.perf_counter()-start
    result=json.loads(process.stdout)
    result['function_seconds']=result['seconds'];result['seconds']=total
    return result


def main():
    with tempfile.TemporaryDirectory(prefix='b6-offline-') as directory:
        root=Path(directory)
        fixture=SourceFixture(root,rows_per_symbol=12000,observations=149)
        try:
            run='live-0b-b6-benchmark'
            auth=propose(run,fixture.last,fixture.source,b3.rules(),now_ms=fixture.last-14400000)
            control,ledger=paths(run,root);control.mkdir(parents=True);ledger.mkdir(parents=True)
            value=auth.record;value['implementations'].update(b4=OLD_B4,b5=OLD_B5)
            exclusive(control/'activation.json',dict(authorization=value,authorization_fingerprint=fingerprint(value)))
            binding=AcceptanceBinding(DraftManifest(fixture.source,rules_fingerprint=auth.rules.id),
                                      ProspectiveGate(value['t0_ms'],fingerprint(value)))
            identity=dict(label=LABEL,version='paper-b4-ledger-v1',implementation=OLD_B4,historical=HISTORICAL,
                binding=binding.record,rules=auth.rules.record,activation='UNSET_FOUNDATION_ONLY',initial_equity='1000',
                initial_equity_availability=dict(policy='EXPLICIT_FUTURE_ACTIVATION_BOUNDARY',available_at_ms=value['t0_ms']))
            db=sqlite3.connect(ledger/'paper.sqlite');db.executescript(SCHEMA)
            db.execute('INSERT INTO identity VALUES(1,?,?)',(fingerprint(identity),encoded(identity).decode()))
            for table in IMMUTABLE:
                for operation in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER no_{operation}_{table} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append only'); END")
            for table,rows in Replay(binding,auth.rules,fingerprint(identity)).rows().items():
                for row in rows:db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')',row)
            db.commit();db.close()
            transition=plan(run,root=root,effective_ms=fixture.last,now_ms=fixture.last-600000)
            exclusive(control/'transition-b6-v1.json',transition)
            source=SourceAdapter(fixture.directory,fixture.source)
            def baseline():
                a=source.snapshot();p=source_progress(source);b=source.snapshot()
                if a!=b:raise AssertionError('Synthetic source changed')
                return dict(observations=len(a),candles=sum(x['count'] for x in p.values()))
            before=[measure(baseline) for _ in range(3)]
            after=[fresh_process(root,run,fixture.last+400000) for _ in range(20)]
            times=sorted(x['seconds'] for x in after)
            report=dict(workload=dict(candles=36000,observations=149,receipts=149,financial_exposure='EMPTY'),
                before_scope='Existing B5 empty-path repeated source verification (32 Git / 8 scans); financial path excluded',
                after_scope='Fresh Python process including imports/startup, legacy root, immutable transition, checkpoint and B4 reconciliation',
                before=before,after=after,before_median=statistics.median(x['seconds'] for x in before),
                after_median=statistics.median(times),after_p95=times[math.ceil(.95*len(times))-1],after_max=max(times))
            report['passes']=all(x['git_processes']==1 and x['full_source_passes']==1 for x in after) and report['after_p95']<=15 and report['after_max']<=30
            print(json.dumps(report,sort_keys=True),flush=True)
            if len(__import__('sys').argv)>1:
                Path(__import__('sys').argv[1]).write_text(json.dumps(report,sort_keys=True,indent=2)+'\n',encoding='utf-8')
        finally:fixture.close()

if __name__=='__main__':main()
