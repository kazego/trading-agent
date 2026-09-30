"""Synthetic B6 workload builder. Never opens the live artifact namespace."""
from pathlib import Path
from unittest.mock import patch
from forward import manifest as fm,operations as fo
from forward.store import Store
from paper.model import SourceIdentity,SourceObservation
from research.spec import encoded,fingerprint
from backtest.data import Candle,SYMBOLS
import test_paper_foundation as b1


class SourceFixture:
    def __init__(self, root, *, rows_per_symbol=9620, observations=1):
        self.directory=Path(root)/'artifacts/forward-paper/live-0a-001'
        self.directory.mkdir(parents=True)
        spec=fm.specification();spec['implementation']='482fec92afbbb96d2f95baf418a22dac442798b805d1e7a44ae4805400746511'
        self.manifest=dict(spec=spec,t0_ms=1789156800000,created_ms=1789151370290,
                          history_start_ms=1780516800000,first_observation_ms=b1.T)
        self.manifest['manifest_fingerprint']=fingerprint(self.manifest)
        assert self.manifest['manifest_fingerprint']==fo.LEGACY_MANIFEST
        (self.directory/'manifest.json').write_bytes(encoded(self.manifest)+b'\n')
        store=Store(self.directory,self.manifest)
        cp=fo.checkpoint(store.db)
        body=dict(label=spec['label'],version='operations-v1',manifest_fingerprint=fo.LEGACY_MANIFEST,
                  implementation=fo.PATCH_OLD,effective_boundary_ms=b1.T,policy=fo.policy(),
                  policy_fingerprint=fingerprint(fo.policy()),checkpoint=cp)
        self.root=dict(body,operations_fingerprint=fingerprint(body))
        self.root_patch=patch.object(fo,'PATCH_ROOT',self.root['operations_fingerprint']);self.root_patch.start()
        try:
            body=fo._patch_body(self.root,cp,b1.T)
            self.patch=dict(body,patch_fingerprint=fingerprint(body))
            (self.directory/'operations-v1.json').write_bytes(encoded(self.root)+b'\n')
            (self.directory/'operations-patch-v1.json').write_bytes(encoded(self.patch)+b'\n')
            self.source=SourceIdentity(fo.LEGACY_MANIFEST,self.root['operations_fingerprint'],
                                      self.patch['patch_fingerprint'],fm.implementation(),b1.T,b1.T)
            start=self.manifest['history_start_ms']
            for symbol in SYMBOLS:
                candles=(Candle(symbol,'15m',t,t+899999,'100','101','99','100','1')
                         for t in range(start,start+rows_per_symbol*900000,900000))
                store.append(symbol,candles,received_ms=start+rows_per_symbol*900000+1)
            self.last=b1.T
            for i in range(observations):
                t=b1.T+i*14400000
                o=b1.observation(self.source);o.update(boundary_ms=t,observed_ms=t+180000)
                for d in o['decisions']:
                    d.update(boundary_ms=t,completed_through_ms=t-1)
                    d['signal_fingerprint']=fingerprint({k:v for k,v in d.items() if k!='signal_fingerprint'})
                store.observation(o)
                r=b1.receipt(o);r.update(boundary_ms=t,observed_ms=t+180001)
                store.db.execute('INSERT INTO events(payload) VALUES(?)',(encoded(r).decode(),));store.db.commit()
                self.last=t
        finally:store.close()

    def close(self):self.root_patch.stop()
