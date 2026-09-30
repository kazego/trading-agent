"""One detached LIVE-0A read transaction per invocation; no cross-run cache.

LIVE-0A code and its operational authorization remain unchanged. This verifier
performs the same manifest/root/patch checks using one explicit historical blob
batch, then invokes the existing full-store validator exactly once.
"""
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import hashlib
import platform
import time
from importlib.metadata import version

from backtest.data import Candle, SYMBOLS
from forward import manifest as fm, operations as fo
from forward.readout import connect, records
from forward.model import HISTORY, FOUR_HOURS
from paper.git_batch import blobs
from paper.market import CandleEvidence
from paper.model import SourceObservation, parse
from research.spec import encoded, fingerprint


def _text(blob):
    return blob.decode('utf-8').replace('\r\n', '\n')


def _authorization(directory, expected, *, timeout):
    fm.verify_references()
    paths = ('manifest.json', 'operations-v1.json', 'operations-patch-v1.json')
    files = tuple((name, (directory/name).read_bytes()) for name in paths)
    m, root, patch = (parse(raw[:-1]) for _, raw in files)
    if any(raw != encoded(value)+b'\n' for (_,raw),value in zip(files,(m,root,patch))):
        raise ValueError('Noncanonical source authorization')
    if set(m) != {'spec','t0_ms','created_ms','history_start_ms','first_observation_ms','manifest_fingerprint'}:
        raise ValueError('Malformed source manifest')
    if (fingerprint({k:v for k,v in m.items() if k != 'manifest_fingerprint'}) != m['manifest_fingerprint']
            or type(m['t0_ms']) is not int or m['t0_ms'] % FOUR_HOURS
            or m['t0_ms'] < m['created_ms']+300000 or m['history_start_ms'] != m['t0_ms']-HISTORY
            or m['first_observation_ms'] != m['t0_ms']+FOUR_HOURS
            or m['manifest_fingerprint'] != fo.LEGACY_MANIFEST or fingerprint(m['spec']) != fo.LEGACY_SPEC):
        raise ValueError('Unrecognized historical manifest')
    requests = [fo.LEGACY_COMMIT+':'+p for p in fo.SEMANTIC_SOURCES]
    requests.append(fo.PATCH_BASE+':forward/operations.py')
    historical = blobs(fm.ROOT, requests, timeout=timeout)
    for p in fo.SEMANTIC_SOURCES:
        if (fm.ROOT/p).read_text(encoding='utf-8') != _text(historical[fo.LEGACY_COMMIT+':'+p]):
            raise ValueError('Frozen source semantics changed')
    sources = {p:hashlib.sha256((fm.ROOT/p).read_text(encoding='utf-8').encode()).hexdigest() for p in fm.SOURCES}
    sources['forward/operations.py'] = hashlib.sha256(_text(historical[requests[-1]]).encode()).hexdigest()
    reconstructed = fingerprint(dict(sources=sources,software=dict(python=platform.python_version(),
                                  pandas=version('pandas'),numpy=version('numpy'))))
    if reconstructed != fo.PATCH_REVIEWED:
        raise ValueError('Source patch scope changed')
    if fo._load_operations_root(directory,m) != root:
        raise ValueError('Source root changed during verification')
    body = fo._patch_body(root,patch['checkpoint'],patch['effective_ms'])
    if patch != dict(body,patch_fingerprint=fingerprint(body)) or time.time_ns()//1000000 < patch['effective_ms']:
        raise ValueError('Source patch identity/effective time invalid')
    if (m['manifest_fingerprint'] != expected.manifest or m['first_observation_ms'] != expected.first_observation_ms
            or root['operations_fingerprint'] != expected.operations
            or root['effective_boundary_ms'] != expected.operational_effective_ms
            or patch['patch_fingerprint'] != expected.patch or patch['semantic_contract'] != expected.semantic_contract
            or patch['new_implementation'] != expected.implementation or fm.implementation() != expected.implementation):
        raise ValueError('Source differs from explicit binding')
    return m,root,patch,files


_SEAL = object()


@dataclass(frozen=True)
class VerifiedSourceSnapshot:
    source: object
    observations: tuple
    candles: object
    progress_bytes: bytes
    checkpoint_bytes: bytes
    directory: Path
    authorization_files: tuple
    implementation: str
    _seal: object

    def __post_init__(self):
        if self._seal is not _SEAL:
            raise ValueError('Snapshot must be captured by the full verifier')

    @property
    def progress(self): return parse(self.progress_bytes)

    @property
    def id(self):
        return fingerprint(dict(source=self.source.id, checkpoint=parse(self.checkpoint_bytes)))

    def snapshot(self): return self.observations

    def at(self, symbol, stamp):
        if symbol not in SYMBOLS or type(stamp) is not int or stamp % 900000:
            raise ValueError('Exact configured candle required')
        return self.candles.get((symbol,stamp))

    def guard(self):
        # No database reread: appended observations/receipts belong to NEXT invocation.
        # Authorization and current source files are never trusted by mtime/size.
        if (self.directory/'BLOCKED.json').exists():
            raise ValueError('Source is BLOCKED')
        if any((self.directory/name).read_bytes() != raw for name,raw in self.authorization_files):
            raise ValueError('Source authorization changed during invocation')
        if fm.implementation() != self.implementation:
            raise ValueError('Source implementation changed during invocation')
        fm.verify_references()


def capture(directory, expected, *, timeout=15):
    directory = Path(directory).resolve(strict=True)
    if (directory/'BLOCKED.json').exists(): raise ValueError('Source is BLOCKED')
    m,root,patch,files = _authorization(directory,expected,timeout=timeout)
    with connect(directory,m) as db:  # BEGIN establishes one consistent SQLite snapshot.
        fo.verify_checkpoint(db,root['checkpoint'])
        fo.verify_checkpoint(db,patch['checkpoint'])
        if any(parse(r[0].encode()).get('event') == 'BLOCKED' for r in db.execute('SELECT payload FROM events')):
            raise ValueError('Source is BLOCKED')
        observations = []
        for value,_ in records(db,root):
            receipt = value.pop('persistence_receipt',None); value.pop('effective_timing')
            observations.append(SourceObservation(expected,encoded(value),encoded(receipt) if receipt else None))
        candles = {}
        for symbol,stamp,payload,digest,received in db.execute('SELECT symbol,open_ms,payload,digest,received_ms FROM candles ORDER BY symbol,open_ms'):
            candle = Candle(**parse(payload.encode()))
            if (candle.symbol,candle.open_time_ms) != (symbol,stamp): raise ValueError('Wrong candle key')
            candles[symbol,stamp] = CandleEvidence(expected,candle,digest,received)
        progress = {}
        for symbol in SYMBOLS:
            stamps = [t for s,t in candles if s == symbol]
            if stamps and (stamps[0]%900000 or len(stamps) != (stamps[-1]-stamps[0])//900000+1):
                raise ValueError('Source continuity failure')
            progress[symbol] = dict(first_open_ms=stamps[0] if stamps else None,
                                   last_open_ms=stamps[-1] if stamps else None,count=len(stamps))
        cp = encoded(fo.checkpoint(db))
    result = VerifiedSourceSnapshot(expected,tuple(observations),MappingProxyType(candles),encoded(progress),
                                    cp,directory,files,expected.implementation,_SEAL)
    result.guard()
    return result
