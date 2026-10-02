"""Reproduce baseline identities from fixed Git blobs; no runtime artifact access."""
import hashlib
from pathlib import Path
from paper.git_batch import blobs
from paper.acceptance import implementation as b2
from paper.journal import HISTORICAL
from paper.operations import policy
from research.spec import fingerprint

BASE = '1800875f40158c0fbfbff2d221a907a51bd8dfe2'

def historical_identities():
    paths3=('paper/market.py','paper/settlement.py','backtest/simulation.py','research/protection.py')
    paths4=('paper/journal.py','paper/ledger.py')
    paths5=('paper/operations.py','paper/consumer.py','paper/readout.py','paper/__main__.py',
        'scripts/live0b-task.ps1','paper/git_batch.py','paper/verified_source.py','paper/transition.py')
    data=blobs(Path(__file__).resolve().parents[1],[BASE+':'+p for p in paths3+paths4+paths5])
    hashes={p:hashlib.sha256(data[BASE+':'+p].decode('utf-8').replace('\r\n','\n').encode()).hexdigest()
            for p in paths3+paths4+paths5}
    b3=fingerprint(dict(version='paper-b3-implementation-v1',b2=b2(),sources={p:hashes[p] for p in paths3}))
    b4=fingerprint(dict(version='paper-b4-v1',historical=HISTORICAL,settlement=b3,sources={p:hashes[p] for p in paths4}))
    b5=fingerprint(dict(version='paper-b5-implementation-v1',b4=b4,policy=policy(),sources={p:hashes[p] for p in paths5}))
    return dict(b3=b3,b4=b4,b5=b5)
