"""Memory-only financial workload; preserves existing v2 fixture confirmation.

Not a production clock-v3 authorization or source-loader benchmark. Historical
Replay is advanced once per event for differential checks outside timed samples;
complete audit runs only after the workload. No financial formulas are replaced.
"""
import argparse
from collections import defaultdict
from decimal import Decimal
import json
import sys
import time

from benchmark_incremental_memory import Meter, TimedConnection, isolation, stats
from test_transactional_bar import fixture, b2
from research.incremental_ledger import SyntheticLedger, ExternalMemory, compact
from research.spec import fingerprint, encoded
from paper.ledger import PaperLedger
from paper.market import CandleEvidence
from paper.model import LABEL
from backtest.data import Candle


def run(cycles=60, modes=('TP','SL'), emit=lambda row:print(json.dumps(row),flush=True)):
    f,v,owner,stamp,candles=fixture(bars=1,accepts=3,backlog=2)
    db=v._db;external=ExternalMemory();observations={};samples=defaultdict(list)
    def evidence(kind,data):
        if kind=='ACCEPT':
            boundary=data['source']['observation']['boundary_ms']
            if observations[boundary]!=data['source']:raise ValueError('Synthetic observation mismatch')
        elif kind in ('ENTRY','BAR'):
            c=data['candle']['candle']
            if candles[c['symbol'],c['open_time_ms']].record!=data['candle']:raise ValueError('Synthetic candle mismatch')
    def construct():
        return SyntheticLedger(v,external,authorization_id='synthetic-financial',
            authorize=lambda:'synthetic-financial',evidence=evidence,clock=lambda:100)
    def make(stamp,opening,high,low,close):
        c=Candle('BTCUSDT','15m',stamp,stamp+899999,*map(str,(opening,high,low,close)),'1')
        item=CandleEvidence(f.binding.draft.source,c,fingerprint(c.record()),stamp+900000)
        candles['BTCUSDT',stamp]=item
        return item.record
    try:
        reference=v._verify();construct().bootstrap()
        sequence=db.execute('SELECT count(*) FROM journal').fetchone()[0]
        def step(kind,data,label,*,retry=False,measure=True):
            nonlocal sequence
            meter=Meter();v._db=TimedConnection(db,meter);old_publish=external.publish
            meter.wrap(external,'publish','publication')
            t=time.perf_counter()
            with meter.span('initialization'):core=construct()
            with meter.span('transition'):result=core.apply(kind,data)
            duration=time.perf_counter()-t
            external.publish=old_publish;v._db=db
            if not retry:
                request=PaperLedger._request(reference,kind,data);parent=reference.journal_head
                expected=reference.apply(kind,data)
                event=dict(label=LABEL,parent=parent,request=request,kind=kind,input=data,result=expected)
                digest=fingerprint(event);sequence+=1
                reference.journal_head=digest;reference.event_ids[request]=digest
                assert result==expected
                assert db.execute('SELECT * FROM journal WHERE sequence=?',(sequence,)).fetchone()==(
                    sequence,digest,request,parent,encoded(event).decode())
            assert external.anchor['sequence']==sequence
            assert external.anchor['state']==compact(reference)
            if measure:
                record=dict(type='sample',scenario=label,total_s=duration,stages=dict(meter.rows),
                            journal_count=sequence,compact_bytes=len(encoded(external.anchor)))
                samples[label].append(duration);emit(record)
            return result
        # Close the seed fixture outside measurement. Subsequent cycles all
        # begin with realized prior-trade accounting rather than fresh equity.
        p=reference.positions[owner];price=p.take_profit
        step('BAR',dict(owner=owner,candle=make(stamp,price,price,price,price)),
             'seed',measure=False)
        for i in range(cycles):
            boundary=max(reference.batches)+14400000
            direction='LONG' if i%2==0 else 'SHORT';mode=modes[i%len(modes)]
            item=b2.item(source=f.binding.draft.source,boundary=boundary,signals=(direction,'NONE','NONE'))
            observations[boundary]=item.record
            decision=step('ACCEPT',dict(source=item.record,accepted_ms=boundary+240000,equity=reference.basis),'ACCEPT')
            owner=decision['reservation']['reservation_id']
            confirmation=step('CONFIRM',dict(owner=owner,durable_ms=boundary+240001,elapsed_ns=1000000),'CONFIRM_V2')
            assert confirmation['reservation_state']=='PENDING_ENTRY'
            entry=boundary+900000;c=make(entry,100,101,99,100)
            opened=step('ENTRY',dict(owner=owner,candle=c),'ENTRY')
            assert opened['status']=='OPEN'
            step('BAR',dict(owner=owner,candle=c),'BAR_OPEN')
            p=reference.positions[owner];stop=p.stop_loss;target=p.take_profit
            if mode=='SL':o=Decimal(100);high=max(o,stop);low=min(o,stop)
            elif mode=='TP':o=Decimal(100);high=max(o,target);low=min(o,target)
            elif mode=='DUAL':o=Decimal(100);high=max(o,stop,target);low=min(o,stop,target)
            elif mode=='GAP_SL':o=stop+(Decimal(-1) if direction=='LONG' else Decimal(1));high=low=o
            else:raise ValueError('Unknown synthetic scenario')
            data=dict(owner=owner,candle=make(entry+900000,o,high,low,o))
            closed=step('BAR',data,'CLOSE_'+mode)
            assert closed['status']=='CLOSED'
            trade=reference.trades[owner]
            assert trade.reason==('TAKE_PROFIT' if mode=='TP' else 'STOP_LOSS')
            assert trade.entry_slippage_cost>0 and trade.exit_slippage_cost>0
            assert trade.position.entry.fee>0 and trade.exit.fee>0
            before=encoded(external.anchor)
            assert step('BAR',data,'RETRY_CLOSED',retry=True)==closed
            assert encoded(external.anchor)==before
        started=time.perf_counter();audit=construct().audit();audit_s=time.perf_counter()-started
        state=v._verify() if cycles==0 else reference
        assert state.equity==Decimal('1000')+sum((t.net_pnl for t in state.trades.values()),Decimal(0))
        for table,rows in state.rows().items():
            if table=='resolutions' and not rows:continue
            assert sorted(db.execute('SELECT * FROM '+table).fetchall())==sorted(rows)
        fees=sum((t.position.entry.fee+t.exit.fee for t in state.trades.values()),Decimal(0))
        result=dict(type='financial_summary',cycles=cycles,confirmation='historical-v2 fixture; no production authorization',
            latency={name:stats(values) for name,values in samples.items()},audit='PASS',audit_s=audit_s,
            journal_count=sequence,closed_trades=len(state.trades),fills=2*len(state.trades),
            equity=str(state.equity),fees=str(fees),net_pnl=str(sum((t.net_pnl for t in state.trades.values()),Decimal(0))),
            slot=state.slot[0],financial_equivalence='full Replay and all SQL projections exact')
        emit(result);return result
    finally:
        v._db=db;f.doCleanups()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cycles',type=int,default=60);args=parser.parse_args()
    if args.cycles<60:parser.error('60 cycles required for 30 TP and 30 SL samples')
    sys.dont_write_bytecode=True;sys.addaudithook(isolation)
    run(args.cycles)
