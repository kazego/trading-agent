"""Offline aggregation and data-access boundaries; no signals are evaluated."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
import unittest

from backtest.data import Candle, Dataset, VERSION as DATA_VERSION, _hash, _encoded
from research.data import (aggregate_bucket, AggregateCandle, SignalDataView, build_view,
                           authorize, STEPS, load_local_development, exact_sum)
from research.spec import Role, SplitRequest, DEV_START, VALIDATION_START, HOLDOUT_START, STEP_MS


def candle(t, symbol="BTCUSDT", price="100", volume="0.1"):
    return Candle(symbol,"15m",t,t+STEP_MS-1,D(price),D(price)+2,D(price)-1,D(price)+1,D(volume))


def request(tf="15m"):
    return SplitRequest(Role.DEVELOPMENT,DEV_START,VALIDATION_START,DEV_START-99*STEPS[tf])


def dataset(candles, warmup=1584, **changes):
    metadata=dict(schema_version=DATA_VERSION,normalization_version=DATA_VERSION,symbols=["BTCUSDT"],
        timeframe="15m",timestamp_unit="ms",start_ms=DEV_START,end_ms=VALIDATION_START,as_of_ms=VALIDATION_START,
        warmup_candles=warmup,duplicate_count=0,excluded_incomplete_count=0,sources=[],
        normalized_candle_fingerprint=_hash([c.record() for c in candles]))
    metadata.update(changes)
    metadata["dataset_fingerprint"]=_hash(metadata)
    return Dataset(tuple(candles),_encoded(metadata).decode("ascii"))


class BucketTests(unittest.TestCase):
    def test_exact_hour(self):
        cs=[candle(DEV_START+i*STEP_MS,price=str(100+i)) for i in range(4)]
        c=aggregate_bucket(cs,"1h")
        self.assertEqual((c.open,c.high,c.low,c.close,c.volume),(D(100),D(105),D(99),D(104),D('.4')))
        self.assertEqual(c.close_time_ms,DEV_START+4*STEP_MS-1)

    def test_exact_four_hour(self):
        c=aggregate_bucket([candle(DEV_START+i*STEP_MS,price=str(100+i)) for i in range(16)],"4h")
        self.assertEqual((c.open,c.high,c.low,c.close,c.volume),(D(100),D(117),D(99),D(116),D('1.6')))

    def test_decimal_sum_ignores_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec=3
            self.assertEqual(exact_sum([D('123456789.123456789'),D('.000000001')]),D('123456789.123456790'))

    def test_passthrough(self):
        c=candle(DEV_START)
        self.assertEqual(aggregate_bucket([c],"15m").record(),c.record())

    def test_missing_partial_duplicate_gap(self):
        cs=[candle(DEV_START+i*STEP_MS) for i in range(4)]
        for bad in (cs[:-1],cs+[cs[-1]],[cs[0],cs[0],cs[2],cs[3]],[cs[0],cs[2],cs[3],candle(DEV_START+4*STEP_MS)]):
            with self.assertRaises(ValueError):aggregate_bucket(bad,"1h")

    def test_alignment(self):
        with self.assertRaises(ValueError):aggregate_bucket([candle(DEV_START+(i+1)*STEP_MS) for i in range(4)],"1h")
        with self.assertRaises(ValueError):aggregate_bucket([candle(DEV_START+(i+4)*STEP_MS) for i in range(16)],"4h")

    def test_symbol_and_timeframe_mismatch(self):
        cs=[candle(DEV_START+i*STEP_MS) for i in range(4)]
        cs[1]=candle(DEV_START+STEP_MS,"ETHUSDT")
        with self.assertRaises(ValueError):aggregate_bucket(cs,"1h")
        with self.assertRaises(ValueError):aggregate_bucket([aggregate_bucket([candle(DEV_START)],"15m")],"15m")

    def test_invalid_model_values(self):
        for field,value in (("volume",-1),("open",float(100)),("high",D('NaN')),("timeframe","2h")):
            data=candle(DEV_START).record();data[field]=value
            with self.assertRaises(ValueError):AggregateCandle(**data)

    def test_denied_roles_before_data_or_files(self):
        for role,start,end in ((Role.VALIDATION,VALIDATION_START,HOLDOUT_START),(Role.HOLDOUT,HOLDOUT_START,HOLDOUT_START+96*STEP_MS)):
            r=SplitRequest(role,start,end,start-99*STEP_MS)
            with self.assertRaises(PermissionError):build_view(object(),"BTCUSDT","15m",r)

    def test_bad_development_interval(self):
        with self.assertRaises(ValueError):SplitRequest(Role.DEVELOPMENT,DEV_START,HOLDOUT_START,DEV_START-99*STEP_MS)

    def test_history_requirements(self):
        for tf,size in (("15m",99),("1h",396),("4h",1584)):
            self.assertEqual((DEV_START-request(tf).warmup_start_ms)//STEP_MS,size)
            authorize(request(tf),tf)
        with self.assertRaises(ValueError):authorize(request("15m"),"4h")


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=dataset([candle(t) for t in range(DEV_START-1584*STEP_MS,VALIDATION_START,STEP_MS)])
        cls.hour=build_view(cls.source,"BTCUSDT","1h",request("1h"))
        cls.four=build_view(cls.source,"BTCUSDT","4h",request("4h"))

    def test_counts_and_provenance(self):
        self.assertEqual(len(self.hour.evaluation_candles),8784)
        self.assertEqual(len(self.four.evaluation_candles),2196)
        p=self.four.provenance
        self.assertEqual(p['source_15m_dataset_fingerprint'],self.source.fingerprint)
        self.assertEqual(p['warmup_components'],1584)
        self.assertEqual(p['warmup_end_ms'],DEV_START)
        self.assertEqual(p['execution_resolution'],'15m')
        self.assertEqual(_hash({k:v for k,v in p.items() if k!='aggregate_fingerprint'}),self.four.fingerprint)

    def test_no_lookahead_at_noon(self):
        noon=DEV_START+12*3600000
        event=self.four.at_open(noon)
        self.assertEqual(len(event.completed),99)
        self.assertEqual(event.completed[-1].open_time_ms,DEV_START+8*3600000)
        self.assertEqual(event.completed[-1].close_time_ms,noon-1)
        index=99+3
        changed=list(self.four.candles)
        for i in range(index,len(changed)):
            changed[i]=replace(changed[i],high=D(9999),low=D(1),close=D(9000),volume=D(999999))
        self.assertEqual(SignalDataView(tuple(changed),self.four.provenance_json).at_open(noon),event)

    def test_next_bucket_open_not_visible(self):
        event=self.four.at_open(DEV_START)
        changed=list(self.four.candles)
        changed[100]=replace(changed[100],open=D(101))
        self.assertEqual(SignalDataView(tuple(changed),self.four.provenance_json).at_open(DEV_START),event)
        with self.assertRaises(ValueError):self.four.at_open(DEV_START+STEP_MS)

    def test_warmup_and_end_cannot_evaluate(self):
        for t in (DEV_START-4*3600000,VALIDATION_START):
            with self.assertRaises(ValueError):self.four.at_open(t)
        self.assertEqual(len(self.four.at_open(DEV_START).completed),99)

    def test_deterministic_rebuild(self):
        self.assertEqual(self.hour,build_view(self.source,"BTCUSDT","1h",request("1h")))

    def test_insufficient_warmup(self):
        small=dataset(self.source.candles[1584-99:],warmup=99)
        with self.assertRaisesRegex(ValueError,'deficient by 1485'):
            build_view(small,"BTCUSDT","4h",request("4h"))

    def test_missing_source_component(self):
        bad=Dataset(self.source.candles[:-1],self.source.provenance_json)
        with self.assertRaises(ValueError):build_view(bad,"BTCUSDT","1h",request("1h"))

    def test_duplicate_source_component(self):
        cs=list(self.source.candles);cs[100]=cs[99]
        with self.assertRaises(ValueError):build_view(Dataset(tuple(cs),self.source.provenance_json),"BTCUSDT","1h",request("1h"))

    def test_fingerprint_tampering(self):
        m=self.source.provenance;m['normalized_candle_fingerprint']='0'*64
        with self.assertRaises(ValueError):build_view(Dataset(self.source.candles,_encoded(m).decode()),"BTCUSDT","1h",request("1h"))
