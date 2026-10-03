"""Fresh-process benchmark of synthetic bound successor invocations only."""
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from artifact_fixture import fixture,bind_clock
from b6_fixture import SourceFixture

CHILD = r"""
import json,sys,tempfile,time,subprocess
from pathlib import Path
from unittest.mock import patch
from forward import operations as fo
from forward.store import Store
from paper import authorization as a
from paper.consumer import consume
root=Path(sys.argv[1]);now=int(sys.argv[2]);run='live-0b-binding-benchmark'
assert root.parent==Path(tempfile.gettempdir()) and root.name.startswith('binding-offline-')
meta=json.loads((root/'artifacts/forward-paper/live-0a-001/operations-v1.json').read_bytes())
verify=Store.verify;process=subprocess.run
with patch.object(fo,'PATCH_ROOT',meta['operations_fingerprint']),patch.object(a,'now_ms',return_value=now),patch('paper.git_batch.subprocess.run',wraps=process) as git,patch.object(Store,'verify',autospec=True,side_effect=verify) as scans:
 result=consume(run,root=root,clock=lambda:now)
 print(json.dumps(dict(git=git.call_count,scans=scans.call_count,result=result,flags=git.call_args.kwargs['creationflags'])))
"""


def main():
    with tempfile.TemporaryDirectory(prefix='binding-offline-') as directory:
        root=Path(directory);source=SourceFixture(root,rows_per_symbol=12000,observations=149)
        try:
            f=fixture(root,'live-0b-binding-benchmark',source=source.source,t0=source.last)
            effective=source.last+14400000;bind_clock(f,effective)
            results=[]
            for _ in range(10):
                start=time.perf_counter()
                p=subprocess.run([sys.executable,'-B','-c',CHILD,str(root),str(effective+400000)],
                    capture_output=True,check=True,timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                row=json.loads(p.stdout);row['seconds']=time.perf_counter()-start;results.append(row)
            values=sorted(x['seconds'] for x in results)
            report=dict(workload=dict(candles=36000,observations=149),fresh_processes=10,
                p50=statistics.median(values),p95=values[math.ceil(.95*len(values))-1],maximum=max(values),runs=results)
            report['passes']=report['p95']<=15 and report['maximum']<=30 and all(
                r['git']==1 and r['scans']==1 and (os.name!='nt' or r['flags']==subprocess.CREATE_NO_WINDOW) for r in results)
            print(json.dumps(report,indent=2))
            if not report['passes']:raise SystemExit(1)
        finally:source.close()

if __name__=='__main__':main()
