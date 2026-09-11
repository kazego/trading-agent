"""LIVE-0A offline only. Synthetic 2024 fixtures; every HTTP call is mocked."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd

from backtest.data import Candle, SYMBOLS, STEP_MS
from forward import model as m, public_feed as feed, manifest, observer
from forward.store import Store
from research.data import aggregate_bucket
from research.features import _atr
from research.spec import fingerprint, encoded
from strategy import get_signal

T = 1704067200000  # Synthetic only. Never read any market-data artifact.


def candles(symbol='BTCUSDT', *, start=T-m.HISTORY, count=9600, closes=None):
    values = closes or [D(100)]*600
    return tuple(Candle(symbol,'15m',start+i*STEP_MS,start+(i+1)*STEP_MS-1,
        D(100),max(D(102),values[min(i//16,len(values)-1)]),
        min(D(98),values[min(i//16,len(values)-1)]),
        values[min(i//16,len(values)-1)],D('0.1')) for i in range(count))


class KernelTests(unittest.TestCase):
    def test_equivalence_long_short_none_equality(self):
        for tail, expected in (([D(90),D(110)],'LONG'),([D(110),D(90)],'SHORT'),
                               ([D(100),D(100)],'NONE'),([D(100),D(101)],'LONG'),
                               ([D(100),D(99)],'SHORT')):
            values=[D(100)]*598+tail
            cs=candles(closes=values)
            snapshot=m.signal_snapshot(cs,'BTCUSDT',T)
            self.assertEqual(snapshot['signal'],expected)
            for forming in (1,100,100000):
                frame=pd.DataFrame({'close':[float(v) for v in values[-99:]]+[forming]})
                before=frame.copy(deep=True)
                self.assertEqual(snapshot['signal'],get_signal(frame))
                pd.testing.assert_frame_equal(before,frame)
            self.assertEqual(snapshot['ema_completed_opens'][0],T-99*m.FOUR_HOURS)
            self.assertEqual(snapshot['ema_completed_opens'][-1],T-m.FOUR_HOURS)

    def test_atr_matches_frozen_calculation(self):
        cs=candles(closes=[D(100+i%7) for i in range(600)])
        bars=tuple(aggregate_bucket(cs[i:i+16],'4h') for i in range(0,len(cs),16))
        self.assertEqual(D(m.signal_snapshot(cs,'BTCUSDT',T)['atr14']),_atr(bars[-100:]))

    def test_semantic_contract_v2_golden_features(self):
        golden=json.loads((manifest.ROOT/'research/semantic_contract_v2.json').read_bytes())['golden']['features']
        cs=tuple(Candle('BTCUSDT','15m',T-m.HISTORY+i*STEP_MS,T-m.HISTORY+(i+1)*STEP_MS-1,
            D(100),D(102+(i//16)%7),D(98-(i//16)%3),D(100+(i//16)%2),D(1)) for i in range(9600))
        snapshot=m.signal_snapshot(cs,'BTCUSDT',T)
        self.assertEqual(snapshot['atr14'],golden['atr'])
        emas={item['span']:item for item in golden['emas']}
        self.assertEqual(snapshot['ema_previous'],[emas[9]['previous'],emas[21]['previous']])
        self.assertEqual(snapshot['ema_latest'],[emas[9]['latest'],emas[21]['latest']])

    def test_exact_aggregation_and_decimal_volume(self):
        cs=candles(count=16)
        with localcontext() as ctx:
            ctx.prec=2
            bar=aggregate_bucket(cs,'4h')
        self.assertEqual(bar.volume,D('1.6'))
        self.assertEqual((bar.open,bar.high,bar.low,bar.close),(D(100),D(102),D(98),D(100)))

    def test_repeated_rolling_equivalence(self):
        values=[D(100+i%3) for i in range(605)]
        cs=candles(count=605*16,closes=values)
        for n in range(6):
            snapshot=m.signal_snapshot(cs[n*16:(n+600)*16],'BTCUSDT',T+n*m.FOUR_HOURS)
            self.assertEqual(snapshot['signal'],get_signal(pd.DataFrame({'close':
                [float(v) for v in values[n+501:n+600]]+[100]})))

    def test_future_and_forming_not_visible(self):
        cs=candles(count=9616)
        before=m.signal_snapshot(cs[:9600],'BTCUSDT',T)
        future=tuple(replace(c,high=D(10000),close=D(9000),volume=D(999)) for c in cs[9600:])
        self.assertEqual(before,m.signal_snapshot((cs[:9600]+future)[:9600],'BTCUSDT',T))
        with self.assertRaises(ValueError): m.signal_snapshot(cs,'BTCUSDT',T)

    def test_missing_duplicate_gap_order_symbol_and_boundary(self):
        cs=candles()
        for invalid in (cs[:-1],cs[::-1],cs[:-1]+(cs[-2],),cs[1:]+(cs[-1],)):
            with self.assertRaises(ValueError): m.signal_snapshot(invalid,'BTCUSDT',T)
        with self.assertRaises(ValueError): m.signal_snapshot(cs,'ETHUSDT',T)
        with self.assertRaises(ValueError): m.signal_snapshot(cs,'BTCUSDT',T+STEP_MS)

    def test_context_is_completed_and_descriptive(self):
        boundary=T+3*m.FOUR_HOURS
        result=m.context_snapshot(candles(start=boundary-m.HISTORY),'BTCUSDT',boundary)
        self.assertEqual(result['values']['1D']['close_time_ms'],T-1)
        self.assertEqual(result['values']['1H']['close_time_ms'],boundary-1)
        self.assertEqual(result['values']['15M']['close_time_ms'],boundary-1)
        self.assertTrue(all(v['role']=='DESCRIPTIVE_ONLY' for v in result['values'].values()))

    def test_deterministic_immutable_inputs(self):
        cs=candles()
        original=cs[-1].record()
        self.assertEqual(m.signal_snapshot(cs,'BTCUSDT',T),m.signal_snapshot(cs,'BTCUSDT',T))
        self.assertEqual(cs[-1].record(),original)


class PublicTests(unittest.TestCase):
    def test_exact_get_host_no_headers_or_redirect(self):
        connection=MagicMock()
        connection.getresponse.return_value.status=200
        connection.getresponse.return_value.read.return_value=b'{"serverTime":1704067200000}'
        with patch.object(feed.http.client,'HTTPSConnection',return_value=connection) as constructor:
            self.assertEqual(feed.server_time(),T)
        constructor.assert_called_once_with('fapi.binance.com',timeout=10)
        connection.request.assert_called_once_with('GET','/fapi/v1/time',headers={'Accept':'application/json'})
        connection.getresponse.return_value.status=302
        with patch.object(feed.http.client,'HTTPSConnection',return_value=connection):
            with self.assertRaisesRegex(ValueError,'Public market-data request failed'): feed.server_time()

    def test_no_raw_error_leakage(self):
        with patch.object(feed.http.client,'HTTPSConnection') as constructor:
            constructor.return_value.request.side_effect=RuntimeError('SECRET_SENTINEL')
            with self.assertRaises(ValueError) as caught: feed.server_time()
            self.assertNotIn('SECRET_SENTINEL',str(caught.exception))

    def test_public_route_denied_before_network(self):
        with patch.object(feed.http.client,'HTTPSConnection') as constructor:
            for path in ('https://example.com','/fapi/v1/unknown','/fapi/v1/time?extra=true'):
                with self.assertRaises(ValueError): feed._read(path)
            constructor.assert_not_called()

    def test_closed_12_column_parsing(self):
        row=[T,'100','102','98','101','0.1',T+STEP_MS-1,'0',1,'0','0','0']
        with patch.object(feed,'_read',return_value=[row]) as read:
            result=feed.closed_klines('BTCUSDT',T,T+STEP_MS,now=T+STEP_MS)
            self.assertEqual(result[0].close,D(101))
            self.assertIn('interval=15m',read.call_args.args[0])
            self.assertIn('endTime='+str(T+STEP_MS-1),read.call_args.args[0])

    def test_malformed_incomplete_float_and_units_rejected(self):
        row=[T,'100','102','98','101','0.1',T+STEP_MS-1,'0',1,'0','0','0']
        for body in ([],[row[:-1]],[row,row],[[T//1000]+row[1:]],[[T,100.0]+row[2:]]):
            with patch.object(feed,'_read',return_value=body):
                with self.assertRaises(ValueError): feed.closed_klines('BTCUSDT',T,T+STEP_MS,now=T+STEP_MS)
        with patch.object(feed,'_read') as read:
            with self.assertRaises(ValueError): feed.closed_klines('BTCUSDT',T,T+STEP_MS,now=T+1)
            read.assert_not_called()

    def test_import_has_no_sdk_config_or_network(self):
        script="import sys; import forward.observer; assert not any(x in sys.modules for x in ('binance','binance_client','config','bot','strategy','backtest.simulation'))"
        result=subprocess.run([sys.executable,'-c',script],cwd=manifest.ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_request_page_bound(self):
        with patch.object(feed,'_read',return_value=[]) as read:
            with self.assertRaises(ValueError):
                feed.closed_klines('BTCUSDT',T,T+2000*STEP_MS,now=T+2000*STEP_MS)
            self.assertIn('endTime='+str(T+1000*STEP_MS-1),read.call_args.args[0])


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'run'
        self.meta=manifest.freeze(self.path,t0=T,created_ms=T-600000)
        self.store=Store(self.path,self.meta)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def fill(self):
        for symbol in SYMBOLS:
            self.store.append(symbol,candles(symbol,count=9616),received_ms=T+m.FOUR_HOURS)

    def test_manifest_immutable_and_implementation_bound(self):
        self.assertEqual(manifest.load(self.path),self.meta)
        with self.assertRaises(FileExistsError): manifest.freeze(self.path,t0=T,created_ms=T-600000)
        with patch.object(manifest,'implementation',return_value='changed'):
            with self.assertRaises(ValueError): manifest.load(self.path)

    def test_manifest_past_or_unaligned_denied(self):
        for t0,now in ((T,T),(T+1,T-600000)):
            with self.assertRaises(ValueError): manifest.freeze(self.path/'other',t0=t0,created_ms=now)

    def test_restart_and_context_cannot_change_decisions(self):
        self.fill()
        self.assertEqual(observer.observe_ready(self.store,observed_ms=T+m.FOUR_HOURS),1)
        raw=self.store.db.execute('SELECT payload FROM observations').fetchone()[0]
        record=json.loads(raw)
        expected=[m.signal_snapshot(self.store.history(s,T+m.FOUR_HOURS),s,T+m.FOUR_HOURS) for s in SYMBOLS]
        self.assertEqual(record['decisions'],expected)
        self.assertEqual(record['timing'],'ONLINE')
        self.store.close(); self.store=Store(self.path,manifest.load(self.path))
        with patch.object(observer,'signal_snapshot',side_effect=AssertionError('duplicate evaluation')):
            self.assertEqual(observer.observe_ready(self.store,observed_ms=T+2*m.FOUR_HOURS),0)
        self.assertEqual(self.store.db.execute('SELECT payload FROM observations').fetchone()[0],raw)

    def test_context_mutation_separate_signal_identity(self):
        self.fill()
        with patch.object(observer,'context_snapshot',return_value={'DESCRIPTIVE_ONLY':'altered'}):
            observer.observe_ready(self.store,observed_ms=T+m.FOUR_HOURS)
        record=json.loads(self.store.db.execute('SELECT payload FROM observations').fetchone()[0])
        for s,decision in zip(SYMBOLS,record['decisions']):
            self.assertEqual(decision,m.signal_snapshot(self.store.history(s,T+m.FOUR_HOURS),s,T+m.FOUR_HOURS))

    def test_history_only_and_all_symbol_barrier(self):
        for s in SYMBOLS: self.store.append(s,candles(s),received_ms=T)
        self.assertEqual(observer.observe_ready(self.store,observed_ms=T),0)
        self.store.append('BTCUSDT',candles(start=T,count=16),received_ms=T+m.FOUR_HOURS)
        self.assertEqual(observer.observe_ready(self.store,observed_ms=T+m.FOUR_HOURS),0)

    def test_delay_disclosed(self):
        self.fill()
        observer.observe_ready(self.store,observed_ms=T+2*m.FOUR_HOURS)
        record=json.loads(self.store.db.execute('SELECT payload FROM observations').fetchone()[0])
        self.assertEqual(record['timing'],'DELAYED_RECONSTRUCTED')

    def test_redelivery_conflict_rollback_and_open_rejection(self):
        cs=candles(count=2)
        self.store.append('BTCUSDT',cs,received_ms=T)
        self.store.append('BTCUSDT',cs,received_ms=T)
        self.assertEqual(self.store.next_candle('BTCUSDT'),cs[-1].open_time_ms+STEP_MS)
        with self.assertRaises(ValueError): self.store.append('BTCUSDT',[replace(cs[0],volume=D(2))],received_ms=T)
        next_cs=candles(start=cs[-1].open_time_ms+STEP_MS,count=3)
        with self.assertRaises(ValueError): self.store.append('BTCUSDT',[next_cs[0],next_cs[2]],received_ms=T)
        self.assertEqual(self.store.next_candle('BTCUSDT'),next_cs[0].open_time_ms)
        with self.assertRaises(ValueError): self.store.append('BTCUSDT',[next_cs[0]],received_ms=next_cs[0].close_time_ms)

    def test_append_only_and_ownership(self):
        with self.assertRaises(Exception): Store(self.path,self.meta)
        with self.assertRaises(sqlite3.IntegrityError): self.store.db.execute("UPDATE identity SET fingerprint='wrong'")
        self.store.db.rollback()

    def test_corruption_rejected_on_restart(self):
        cs=candles(count=1)
        self.store.append('BTCUSDT',cs,received_ms=T)
        self.store.db.execute('DROP TRIGGER no_UPDATE_candles')
        self.store.db.execute("UPDATE candles SET digest='wrong'")
        self.store.db.commit()
        self.store.close()
        with self.assertRaises(ValueError): Store(self.path,self.meta)

    def test_poll_missing_manifest_no_network(self):
        with patch.object(feed,'server_time') as call:
            with self.assertRaises(FileNotFoundError): observer.poll(self.path/'missing')
            call.assert_not_called()

    def test_block_latch_prevents_network_after_failure(self):
        self.store.event('BLOCKED')
        self.store.close()
        with patch.object(feed,'server_time') as call:
            with self.assertRaises(ValueError): observer.poll(self.path)
            call.assert_not_called()

    def test_mocked_poll_only_collects_closed_and_persists_cursor(self):
        self.store.close()
        now=T-300000  # Before T0: acquisition is HISTORY_ONLY, no observations.
        def page(symbol,start,end,*,now):
            count=min(1000,(end-start)//STEP_MS)
            return candles(symbol,start=start,count=count)
        with patch.object(feed,'server_time',return_value=now), \
             patch.object(feed,'closed_klines',side_effect=page) as fetch, \
             patch.object(observer.time,'time_ns',return_value=now*1_000_000):
            result=observer.poll(self.path)
            self.assertEqual(result['observations_written'],0)
            self.assertLessEqual(fetch.call_count,30)
            self.assertEqual(result['collected_through_ms'],now//STEP_MS*STEP_MS-1)
            fetch.reset_mock()
            self.assertEqual(observer.poll(self.path)['observations_written'],0)
            fetch.assert_not_called()

    def test_observation_failure_leaves_no_partial_boundary(self):
        self.fill()
        with patch.object(observer,'context_snapshot',side_effect=ValueError('synthetic')):
            with self.assertRaises(ValueError): observer.observe_ready(self.store,observed_ms=T+m.FOUR_HOURS)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM observations').fetchone()[0],0)
        self.assertEqual(self.store.next_boundary(),T+m.FOUR_HOURS)


if __name__ == '__main__': unittest.main()
