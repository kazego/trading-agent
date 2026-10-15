"""Offline-only stage profiler. Synthetic stores; never imports into paper runtime.

Run with the project interpreter and -B. Output and all SQLite writes are confined
to a newly allocated temporary directory. No configurable production DB input.
"""
import time
SCRIPT_ENTRY_NS = time.perf_counter_ns()

import argparse
from contextlib import ExitStack, contextmanager
from collections import defaultdict
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
RUN = 'live-0b-offline-stages'
PREFIX = 'live0b-stages-'
SQLITE_COVERAGE = {
    'included': 'connect/close; cursor execute/executemany/executescript; fetchone/fetchmany/fetchall/iteration; commit/rollback; context exit',
    'meaning': 'Wall time of Python sqlite3 calls, including SQLite stepping/row conversion inside those calls; NOT physical disk latency alone.',
    'excluded': 'OS physical durability, device cache flush completion, per-row Python replay outside sqlite3 calls; these remain in owning Python stages.',
    'nesting': 'Inclusive stages overlap; exclusive wall-time totals and parent-child edges avoid double counting. Instrumentation overhead remains in timings.',
}


def within(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def allocate_root():
    """Unique disposable root inheriting the sandbox TEMP's existing permissions.

    Windows Python mkdtemp uses mkdir(0o700), which makes newly created folders
    inaccessible to this restricted token. Do not chmod/reset ACLs or change TEMP.
    Ordinary mkdir inherits the already sandbox-private parent's permissions.
    Isolation still confines every child to this exact unpredictable root.
    """
    parent=Path(tempfile.gettempdir()).resolve()
    if os.name=='nt' and not any(p.startswith('sandbox.') for p in parent.parts):
        raise ValueError('This Windows harness requires the sandbox-private TEMP')
    root=parent/(PREFIX+uuid.uuid4().hex)
    root.mkdir(exist_ok=False)
    Isolation(root)
    return root


def remove_root(root):
    """Remove only a verified synthetic root, never adjust permissions on failure."""
    root=Path(root).absolute()
    Isolation(root)
    if root.resolve()!=root or root.is_symlink():
        raise ValueError('Aliased cleanup root forbidden')
    shutil.rmtree(root)


class Isolation:
    """Process audit hook: no live artifacts, network, external DB or writes."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        if self.root.parent != Path(tempfile.gettempdir()).resolve() or not self.root.name.startswith(PREFIX):
            raise ValueError('Only freshly allocated benchmark temporary roots allowed')

    def __call__(self, event, args):
        if event in ('os.system', 'os.startfile', 'os.startfile/2', 'os.exec', 'os.posix_spawn'):
            raise PermissionError('Shell/alternate process launch forbidden')
        if event.startswith('socket.') or event.startswith('urllib.'):
            raise PermissionError('Network forbidden in offline benchmark')
        if event == 'sqlite3.connect':
            value = str(args[0])
            if value.startswith('file:'):
                from urllib.parse import urlsplit, unquote
                parsed = urlsplit(value)
                if parsed.netloc:
                    raise PermissionError('Remote SQLite URI forbidden')
                value = unquote(parsed.path)
                if os.name == 'nt' and value.startswith('/'):
                    value = value[1:]
            if not within(value, self.root):
                raise PermissionError('SQLite outside synthetic root forbidden')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(args[0])).resolve()
            if within(p, REPO/'artifacts'):
                raise PermissionError('Production artifacts cannot even be read')
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(x in mode for x in 'wax+')) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and not within(p, self.root):
                raise PermissionError('Write outside isolated benchmark root forbidden')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod'):
            if not within(args[0], self.root):
                raise PermissionError('Filesystem mutation outside isolated root forbidden')
        if event in ('os.rename', 'os.link', 'os.symlink'):
            if not all(within(p, self.root) for p in args[:2]):
                raise PermissionError('Aliasing/move outside isolated root forbidden')
        if event == 'subprocess.Popen':
            command = args[1]
            if (not isinstance(command, (list, tuple)) or list(command) != ['git', 'cat-file', '--batch']
                    or Path(args[0]).name.lower() not in ('git', 'git.exe')
                    or args[2] is None or Path(args[2]).resolve() != REPO):
                raise PermissionError('Only fixed read-only Git batch child permitted')


class Meter:
    """Nested inclusive and exclusive wall time; exclusive totals do not overlap."""
    def __init__(self):
        self.rows = defaultdict(lambda: dict(calls=0, inclusive_s=0.0, exclusive_s=0.0))
        self.edges = defaultdict(lambda: dict(calls=0, inclusive_s=0.0, exclusive_s=0.0))
        self.stack = []

    @contextmanager
    def span(self, name):
        parent = self.stack[-1][2] if self.stack else '<root>'
        frame = [time.perf_counter_ns(), 0, name]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter_ns() - frame[0]
            self.stack.pop()
            for row in (self.rows[name], self.edges[parent+' -> '+name]):
                row['calls'] += 1
                row['inclusive_s'] += elapsed/1e9
                row['exclusive_s'] += (elapsed-frame[1])/1e9
            if self.stack:
                self.stack[-1][1] += elapsed

    def wrap(self, obj, name, stack, label=None):
        original = getattr(obj, name)
        @functools.wraps(original)
        def measured(*args, **kwargs):
            with self.span(label or f'{obj.__name__}.{name}'):
                return original(*args, **kwargs)
        stack.enter_context(patch.object(obj, name, measured))


def sqlite_instrumentation(meter, stack):
    """Exercise normal SQLite calls; never substitute or skip their work."""
    class TimedCursor(sqlite3.Cursor):
        category = 'ordinary'

        def execute(self, sql, *args, **kwargs):
            upper = sql.strip().upper()
            self.category = ('integrity' if upper.startswith('PRAGMA INTEGRITY_CHECK') else
                             'foreign_keys' if upper.startswith('PRAGMA FOREIGN_KEY_CHECK') else 'ordinary')
            with meter.span('sqlite.execute.'+self.category):
                return super().execute(sql, *args, **kwargs)

        def executemany(self, *args, **kwargs):
            self.category = 'ordinary'
            with meter.span('sqlite.executemany'):
                return super().executemany(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            self.category = 'ordinary'
            with meter.span('sqlite.executescript'):
                return super().executescript(*args, **kwargs)

        def fetchone(self):
            with meter.span('sqlite.fetchone.'+self.category):
                return super().fetchone()

        def fetchmany(self, *args, **kwargs):
            with meter.span('sqlite.fetchmany.'+self.category):
                return super().fetchmany(*args, **kwargs)

        def fetchall(self):
            with meter.span('sqlite.fetchall.'+self.category):
                return super().fetchall()

        def __next__(self):
            with meter.span('sqlite.next.'+self.category):
                return super().__next__()

        def close(self):
            with meter.span('sqlite.cursor_close'):
                return super().close()

    class TimedConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            if args or 'factory' in kwargs:
                raise ValueError('Unexpected cursor factory')
            return super().cursor(factory=TimedCursor)

        def execute(self, *args, **kwargs):
            return self.cursor().execute(*args, **kwargs)

        def executemany(self, *args, **kwargs):
            return self.cursor().executemany(*args, **kwargs)

        def executescript(self, *args, **kwargs):
            return self.cursor().executescript(*args, **kwargs)

        def commit(self):
            with meter.span('sqlite.commit'):
                return super().commit()

        def rollback(self):
            with meter.span('sqlite.rollback'):
                return super().rollback()

        def close(self):
            with meter.span('sqlite.close'):
                return super().close()

        def __exit__(self, *args):
            # sqlite3's context manager may commit internally without calling the override.
            with meter.span('sqlite.context_exit'):
                return super().__exit__(*args)

    connect = sqlite3.connect
    def timed_connect(*args, **kwargs):
        if 'factory' in kwargs or len(args)>5:
            raise ValueError('Unexpected SQLite connection factory')
        kwargs['factory'] = TimedConnection
        with meter.span('sqlite.connect'):
            return connect(*args, **kwargs)
    stack.enter_context(patch.object(sqlite3, 'connect', timed_connect))


def instrumentation(meter, stack):
    from paper import authorization as a, binding, consumer, git_batch, verified_source as vs
    from paper.ledger import PaperLedger
    from paper.journal import Replay
    from forward.store import Store
    for obj, names in (
        (a, ('chain', 'state_at', 'check_prefix', 'verify_successor')),
        (a.ClockCapability, ('guard',)),
        (consumer, ('load_execution', 'capture')),
        (PaperLedger, ('__init__', 'reconcile', '_verify', '_sources', '_source_guard', '_append', '_materialize', 'process_candle')),
        (Replay, ('apply', 'rows')),
        (vs, ('_authorization',)),
        (vs.VerifiedSourceSnapshot, ('guard',)),
        (Store, ('verify',)),
        (binding, ('source_identity',)),
        (git_batch, ('blobs',)),
        (Path, ('read_bytes', 'read_text')),
    ):
        for name in names:
            meter.wrap(obj, name, stack)

    sqlite_instrumentation(meter, stack)


def counts(directory):
    from contextlib import closing
    with closing(sqlite3.connect((directory/'paper.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
        return dict(journal=db.execute('SELECT count(*) FROM journal').fetchone()[0],
            processed=db.execute('SELECT count(*) FROM processed').fetchone()[0],
            positions=db.execute('SELECT count(*) FROM positions').fetchone()[0],
            fills=db.execute('SELECT count(*) FROM fills').fetchone()[0],
            trades=db.execute('SELECT count(*) FROM trades').fetchone()[0],
            equity_events=db.execute('SELECT count(*) FROM equity').fetchone()[0],
            account=db.execute('SELECT amount FROM account').fetchone()[0],
            slot=list(db.execute('SELECT phase,owner,symbol FROM global_slot').fetchone()),
            integrity=db.execute('PRAGMA integrity_check').fetchone()[0])


def build(root, bars, backlog, empty=False):
    """Synthesize valid journal with pure historical Replay, not operational input.

    One short at constant price, no SL/TP touch; all other signals NONE. All
    historical observations are pre-consumed. Only next execution bars remain.
    Fixture construction is excluded from invocation measurements.
    """
    from b6_fixture import SourceFixture
    from artifact_fixture import fixture, bind_clock
    import test_paper_foundation as b1
    import test_paper_acceptance as b2
    from forward.store import Store
    from paper.market import CandleEvidence
    from paper.ledger import PaperLedger
    from paper.journal import Replay
    from paper.model import LABEL
    from research.spec import encoded, fingerprint
    from backtest.data import Candle

    # T is row 9616 relative to the fixture's frozen warm-up origin.
    rows = 9617 + bars + backlog
    observations = 1 + (bars+backlog)//16
    src = SourceFixture(root, rows_per_symbol=rows, observations=0)
    try:
        store = Store(src.directory, src.manifest)
        items = []
        for i in range(observations):
            boundary = b1.T + i*14400000
            signals = ('NONE', 'NONE', 'SHORT') if i == 0 and not empty else ('NONE',)*3
            item = b2.item(source=src.source, boundary=boundary, signals=signals)
            store.observation(item.record['observation'])
            store.db.execute('INSERT INTO events(payload) VALUES(?)', (encoded(item.record['receipt']).decode(),))
            store.db.commit(); items.append(item)
        store.close()
        f = fixture(root, RUN, source=src.source, t0=b1.T-14400000)
        db = sqlite3.connect(f.directory/'paper.sqlite')
        identity_id = db.execute('SELECT digest FROM identity').fetchone()[0]
        state = Replay(f.binding, f.rules, identity_id)
        parent = identity_id
        def event(kind, data):
            nonlocal parent
            request = PaperLedger._request(state, kind, data)
            result = state.apply(kind, data)
            body = dict(label=LABEL, parent=parent, request=request, kind=kind, input=data, result=result)
            digest = fingerprint(body)
            db.execute('INSERT INTO journal VALUES(?,?,?,?,?)',
                (len(state.event_ids)+1, digest, request, parent, encoded(body).decode()))
            state.journal_head = digest; state.event_ids[request] = digest; parent = digest
            return result

        first = event('ACCEPT', dict(source=items[0].record, accepted_ms=b1.T+240000, equity=state.basis))
        end = src.manifest['history_start_ms'] + rows*900000
        now = end+400000
        if not empty:
            owner = first['reservation']['reservation_id']
            event('CONFIRM', dict(owner=owner, durable_ms=b1.T+240001, elapsed_ns=1000000))
            def candle(stamp):
                c = Candle('ADAUSDT','15m',stamp,stamp+899999,'100','101','99','100','1')
                return CandleEvidence(src.source,c,fingerprint(c.record()),end+1).record
            entry = b1.T+900000
            event('ENTRY', dict(owner=owner,candle=candle(entry)))
            for i in range(bars):
                event('BAR', dict(owner=owner,candle=candle(entry+i*900000)))
        for item in items[1:]:
            event('ACCEPT', dict(source=item.record,accepted_ms=now,equity=state.basis))
        # Materialize pure replay once. This is fixture construction only.
        for table, data in state.rows().items():
            if table == 'resolutions' and not data:
                continue
            if table in ('account','global_slot'):
                db.execute('DELETE FROM '+table)
            for row in data:
                db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')', row)
        db.commit(); db.close()
        # The bound checkpoint is historical even within this synthetic chronology.
        effective = ((now+1000000)//14400000+1)*14400000
        now = effective+400000
        bind_clock(f,effective)
        from paper.authorization import state_at
        verified, cp = state_at(RUN,root,f.v)
        if verified.rows() != state.rows():
            raise AssertionError('Fixture replay differs')
        metadata = dict(now=now,bars=bars,backlog=backlog,empty=empty,
            source_candles=3*rows,source_observations=observations,
            patch_root=src.root['operations_fingerprint'],before=counts(f.directory),checkpoint=cp)
        (root/'fixture.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
        return metadata
    finally:
        src.close()


def require_unblocked(control):
    if control.parent.name != 'forward-paper-consumer':
        raise ValueError('Unexpected canonical control directory')
    if (control/'BLOCKED.json').exists():
        raise AssertionError('Synthetic consumer latched BLOCKED')


def child(root, profiled, launch_ns):
    child_entry = time.perf_counter_ns()
    sys.addaudithook(Isolation(root.parent))
    meta = json.loads((root/'fixture.json').read_text())
    from paper import authorization as a, consumer
    from paper.operations import paths
    from forward import operations as fo
    from forward.store import Store
    control, directory = paths(RUN, root)
    require_unblocked(control)
    before = counts(directory)
    meter = Meter()
    process = subprocess.run
    verify = Store.verify
    observed = dict(git=0,source_scans=0,git_flags=[])
    def git(*args, **kwargs):
        observed['git'] += 1; observed['git_flags'].append(kwargs.get('creationflags'))
        return process(*args, **kwargs)
    def scan(self, *args, **kwargs):
        observed['source_scans'] += 1
        return verify(self,*args,**kwargs)
    prepared = time.perf_counter_ns()
    with ExitStack() as stack:
        stack.enter_context(patch.object(fo,'PATCH_ROOT',meta['patch_root']))
        stack.enter_context(patch.object(a,'now_ms',return_value=meta['now']))
        stack.enter_context(patch('paper.git_batch.subprocess.run',git))
        stack.enter_context(patch.object(Store,'verify',scan))
        if profiled:
            instrumentation(meter,stack)
        invocation_start = time.perf_counter_ns()
        with meter.span('invocation'):
            result = consumer.consume(RUN,root=root,clock=lambda:meta['now'])
        invocation_end = time.perf_counter_ns()
    post_start = time.perf_counter_ns()
    after = counts(directory)
    # Full normal reconciliation, outside the timed invocation and without guards bypassed.
    from paper.authorization import chain,state_at
    v = chain(RUN,root)[0]
    state, cp = state_at(RUN,root,v)
    assert after['integrity']=='ok' and after['account']==before['account']=='1000'
    assert after['fills']==before['fills'] and after['trades']==before['trades']==0
    assert after['equity_events']==before['equity_events']==0 and after['slot']==before['slot']
    assert after['journal']-before['journal']==result['transitions']
    assert observed['git']==observed['source_scans']==1
    assert after['positions']==before['positions'] and after['processed']-before['processed']==result['transitions']
    assert result['state'] in ('HEALTHY','WAITING','BUDGET_REACHED')
    assert observed['git_flags']==[subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0]
    require_unblocked(control)
    post_end = time.perf_counter_ns()
    seconds = (invocation_end-invocation_start)/1e9
    from research.spec import fingerprint
    print(json.dumps(dict(seconds=seconds,profiled=profiled,result=result,counts=observed,
        timings=dict(launch_to_script_s=(SCRIPT_ENTRY_NS-launch_ns)/1e9,
            harness_imports_cli_s=(child_entry-SCRIPT_ENTRY_NS)/1e9,
            preparation_imports_precheck_s=(prepared-child_entry)/1e9,
            instrumentation_setup_s=(invocation_start-prepared)/1e9,consumer_s=seconds,
            instrumentation_teardown_s=(post_start-invocation_end)/1e9,
            harness_postcheck_s=(post_end-post_start)/1e9),
        transitions_per_second=result['transitions']/seconds,
        budget_seconds=consumer.policy()['max_runtime_seconds'],
        before=before,after=after,stages=dict(meter.rows),edges=dict(meter.edges),
        reconciliation='PASS',checkpoint=cp,state_fingerprint=fingerprint(state.rows()))))


def inventory(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def sample_summary(values):
    values=sorted(values)
    return dict(n=len(values),p50=statistics.median(values),
        p95_nearest_rank=values[math.ceil(.95*len(values))-1],maximum=max(values),
        limitation='Descriptive small-sample statistics, NOT a robust population p95 estimate')


def compare_pair(plain, profiled):
    if plain['before']!=profiled['before']:
        raise AssertionError('Different initial fixture')
    for row in (plain,profiled):
        for field in ('account','slot','positions','fills','trades','equity_events'):
            if row['after'][field]!=row['before'][field]:
                raise AssertionError('Financial conservation failure: '+field)
    same=(plain['result']==profiled['result'] and plain['state_fingerprint']==profiled['state_fingerprint'])
    if not same and 'BUDGET_REACHED' not in (plain['result']['state'],profiled['result']['state']):
        raise AssertionError('Profiling changed deterministic results without budget exhaustion')
    # A slower instrumented run can legally stop at an earlier committed prefix.
    return dict(exact_state_equivalence=same,
        note='PASS' if same else 'Budget stopped at different prefixes; not claimed equivalent',
        overhead_ratio=profiled['seconds']/plain['seconds']-1 if same else None)


def summarize(results):
    output={}
    for case in sorted({r['case'] for r in results}):
        rows=[r for r in results if r['case']==case]
        groups={str(p):[r for r in rows if r['profiled']==p] for p in (False,True)}
        pairs=[]
        for repeat in sorted({r['repeat'] for r in rows}):
            pair=[r for r in rows if r['repeat']==repeat]
            if len(pair)==2:
                pairs.append(compare_pair(next(r for r in pair if not r['profiled']),next(r for r in pair if r['profiled'])))
        output[case]=dict(groups={k:dict(consumer=sample_summary([r['seconds'] for r in v]),
            process=sample_summary([r['process_seconds'] for r in v]),
            throughput=sample_summary([r['transitions_per_second'] for r in v]),
            outcomes=[r['result'] for r in v]) for k,v in groups.items() if v},pairs=pairs)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', type=Path)
    parser.add_argument('--profiled', action='store_true')
    parser.add_argument('--launch-ns',type=int,default=0)
    parser.add_argument('--bars',default='0,50,200,500')
    parser.add_argument('--backlog',type=int,default=4)
    parser.add_argument('--repeats',type=int,default=3)
    args = parser.parse_args()
    if args.child:
        child(args.child.resolve(), args.profiled,args.launch_ns);return
    bars = [int(x) for x in args.bars.split(',')]
    if any(x<0 or x>4000 for x in bars) or not 1<=args.repeats<=20 or not 0<=args.backlog<=64:
        raise ValueError('Bounded offline workload required')
    directory = allocate_root()
    # Each fixture builder and measured invocation has its own fresh interpreter.
    print('REPORT_DIRECTORY='+str(directory),flush=True)
    provenance=dict(python=sys.version,executable=sys.executable,sqlite=sqlite3.sqlite_version,
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        source_files={str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest()
            for name in ('paper','forward','research','backtest') for p in (REPO/name).glob('*.py')},
        fixture_inputs={name:hashlib.sha256((REPO/'tests'/name).read_bytes()).hexdigest()
            for name in ('b6_fixture.py','artifact_fixture.py','test_paper_foundation.py','test_paper_acceptance.py','test_paper_settlement.py')},
        sqlite_coverage=SQLITE_COVERAGE,synthetic_only=True,live_database_access=False)
    (directory/'provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    results=[]
    cases=[('empty',0,True)]+[(f'open-{n}',n,False) for n in bars]
    for name,n,empty in cases:
        seed=directory/(name+'-seed');seed.mkdir()
        code="import sys;from pathlib import Path;sys.path.insert(0,'tests');from benchmark_live0b_stages import *;root=Path(sys.argv[1]);sys.addaudithook(Isolation(root.parent));build(root,int(sys.argv[2]),int(sys.argv[3]),sys.argv[4]=='1')"
        p=subprocess.run([sys.executable,'-B','-c',code,str(seed),str(n),str(args.backlog),str(int(empty))],
            cwd=REPO,capture_output=True,timeout=600,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if p.returncode:
            raise RuntimeError(p.stderr.decode(errors='replace'))
        original=inventory(seed)
        (directory/(name+'-inventory.json')).write_text(json.dumps(original,indent=2),encoding='utf-8')
        # Alternate paired order to reduce systematic warm-cache/order bias.
        for index,profiled in [(i,p) for i in range(args.repeats) for p in ((False,True) if i%2==0 else (True,False))]:
            clone=directory/(name+f'-run{index}-profile{int(profiled)}');shutil.copytree(seed,clone)
            assert inventory(clone)==original
            command=[sys.executable,'-B',str(Path(__file__).resolve()),'--child',str(clone)]
            if profiled:command.append('--profiled')
            launch_ns=time.perf_counter_ns()
            command.extend(['--launch-ns',str(launch_ns)])
            p=subprocess.run(command,cwd=REPO,capture_output=True,timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            elapsed=(time.perf_counter_ns()-launch_ns)/1e9
            if p.returncode:
                raise RuntimeError(p.stderr.decode(errors='replace'))
            result=json.loads(p.stdout);result.update(case=name,repeat=index,process_seconds=elapsed,
                fixture=json.loads((seed/'fixture.json').read_text()))
            result['timings']['output_exit_s']=elapsed-sum(result['timings'].values())
            if any(v<0 for v in result['timings'].values()):
                raise AssertionError('Nonmonotonic phase timestamps')
            results.append(result)
            (directory/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
            print(json.dumps({k:result[k] for k in ('case','repeat','seconds','process_seconds','result','counts')}),flush=True)
        assert inventory(seed)==original
        (directory/'summary.json').write_text(json.dumps(summarize(results),indent=2),encoding='utf-8')
    print('COMPLETE_REPORT='+str(directory/'results.json'),flush=True)


if __name__=='__main__':
    main()
