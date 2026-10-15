"""Exact audit-time persistence and shared core checks, entirely in memory."""
import json
import math
import unittest
from unittest.mock import patch

from research.incremental_durable import audit_record, audit_value, DurableExternal
from research.incremental_ledger import ExternalMemory, SyntheticLedger, Blocked
from research.spec import encoded, fingerprint
from test_transactional_bar import fixture


class AuditTimeTests(unittest.TestCase):
    def value(self,at):return {'sequence':7,'head':'a'*64,'at':at}

    def test_original_default_clock_failure(self):
        with self.assertRaisesRegex(ValueError,'floats are forbidden'):
            encoded(self.value(1791979200.1234567))

    def test_exact_deterministic_roundtrip(self):
        for at in (0.0,0.1,1791979200.1234567,math.nextafter(1791979200.,math.inf),100):
            original=self.value(at);packed=audit_record(original)
            raw=encoded(packed);restored=audit_value(json.loads(raw))
            self.assertEqual(restored,original);self.assertIs(type(restored['at']),type(at))
            if type(at) is float:self.assertEqual(restored['at'].hex(),at.hex())
            self.assertEqual(encoded(audit_record(restored)),raw)
            self.assertEqual(original,self.value(at))

    def test_actual_external_property_uses_codec(self):
        store=object.__new__(DurableExternal);records=[]
        def append(prefix,value):
            self.assertEqual(prefix,'audit');records.append(json.loads(encoded(value)))
        with patch.object(store,'_append',side_effect=append),patch.object(store,'_chain',side_effect=lambda _: (records,None)):
            self.assertIsNone(store.audit)
            store.audit=self.value(1791979200.1234567)
            self.assertEqual(store.audit,self.value(1791979200.1234567))

    def test_invalid_runtime_samples_rejected(self):
        for at in (True,None,'1',-1,float('nan'),float('inf'),float('-inf')):
            with self.assertRaises(Blocked):audit_record(self.value(at))

    def test_invalid_persisted_samples_rejected(self):
        for at in (True,1.5,-1,{}, {'encoding':'wrong','value':'0x1.0p+0'},
                   {'encoding':'seconds-binary64-hex-v1','value':'nan'},
                   {'encoding':'seconds-binary64-hex-v1','value':'0x1p+0'}):
            with self.assertRaises(Blocked):audit_value(self.value(at))

    def test_floats_elsewhere_still_rejected(self):
        value=self.value(.25);value['sequence']=7.0
        with self.assertRaisesRegex(ValueError,'floats are forbidden'):audit_record(value)
        for body in ({'equity':1000.0},{'input':{'entry':.25}},{'state':{'equity':1000.0}}):
            for serializer in (encoded,fingerprint):
                with self.assertRaisesRegex(ValueError,'floats are forbidden'):serializer(body)

    def test_existing_integer_audit_bytes_unchanged(self):
        value=self.value(100)
        self.assertEqual(encoded(audit_record(value)),encoded(value))
        self.assertEqual(audit_value(value),value)

    def test_bootstrap_transition_audit_and_expiry_without_rounding(self):
        class PersistedAuditMemory(ExternalMemory):
            @property
            def audit(self):
                return audit_value(json.loads(self.audit_bytes)) if getattr(self,'audit_bytes',None) else None
            @audit.setter
            def audit(self,value):self.audit_bytes=encoded(audit_record(value)) if value is not None else None
        f,v,owner,stamp,candles=fixture(bars=2,backlog=4);self.addCleanup(f.doCleanups)
        store=PersistedAuditMemory();now=[1791979200.1234567]
        def evidence(kind,data):
            self.assertEqual(kind,'BAR')
            self.assertEqual(data['candle'],candles['BTCUSDT',data['candle']['candle']['open_time_ms']].record)
        core=SyntheticLedger(v,store,authorization_id='test',authorize=lambda:'test',evidence=evidence,clock=lambda:now[0])
        core.bootstrap();self.assertEqual(store.audit['at'],now[0])
        data=lambda i:dict(owner=owner,candle=candles['BTCUSDT',stamp+i*900000].record)
        core.apply('BAR',data(0));core.audit()
        at=store.audit['at'];now[0]=math.nextafter(at+86400,-math.inf)
        core.apply('BAR',data(1))
        now[0]=at+86400
        with self.assertRaisesRegex(Blocked,'audit expired'):core.apply('BAR',data(2))
        now[0]=math.nextafter(at,-math.inf)
        with self.assertRaisesRegex(Blocked,'audit expired'):core.apply('BAR',data(2))


if __name__=='__main__':unittest.main()
