"""Synthetic clock profiles only. No live DB, authorization, source or task access."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from paper import clock_v3 as v3
from research.spec import encoded, fingerprint

D = 1790871240000

def sample(ms, before=0, after=None):
    return dict(before_ns=before,utc_ms=ms,after_ns=before if after is None else after)

def reservation(ms):
    return dict(label='FORWARD_PAPER / NON_EXECUTING',acceptance_ms=ms,deadline_ms=D,reservation_id='a'*64)

class ClockTests(unittest.TestCase):
    def confirm(self,a,c): return v3.confirmation(reservation(a.get('utc_ms',D-1000)),a,c)

    def test_small_disagreements_both_directions(self):
        for ms in (0,1,5,6,10):
            for sign in (-1,1):
                with self.subTest(ms=ms,sign=sign):
                    r=self.confirm(sample(D-1000),sample(D-600,400000000+sign*ms*1000000))
                    self.assertEqual(r['status'],'PENDING_ENTRY')

    def test_exact_envelope_both_directions_nanosecond_edges(self):
        edge=32250200
        for sign in (-1,1):
            for offset,status in ((-1,'PENDING_ENTRY'),(0,'PENDING_ENTRY'),(1,'BLOCKED_CLOCK')):
                with self.subTest(sign=sign,offset=offset):
                    r=self.confirm(sample(D-1000),sample(D-600,400000000+sign*(edge+offset)))
                    self.assertEqual(r['status'],status)

    def test_50ms_outside_envelope(self):
        for sign in (-1,1):
            self.assertEqual(self.confirm(sample(D-1000),sample(D-600,400000000+sign*50000000))['status'],'BLOCKED_CLOCK')

    def test_utc_regression_even_one_ms(self):
        self.assertEqual(self.confirm(sample(D-100),sample(D-101,1))['clock_error'],'UTC_REGRESSION')

    def test_sample_order_regressions(self):
        for a,c in ((sample(D-100,2,1),sample(D-90,3)),(sample(D-100,0,3),sample(D-90,2,4)),
                    (sample(D-100),sample(D-90,4,3))):
            self.assertEqual(self.confirm(a,c)['status'],'BLOCKED_CLOCK')

    def test_invalid_samples(self):
        for bad in ({},None,dict(before_ns=True,utc_ms=D,after_ns=1),sample(D,-1),sample(float(D)),
                    dict(before_ns=0,utc_ms=D,after_ns=0,extra=1)):
            self.assertEqual(v3.confirmation(reservation(D-1000),sample(D-1000),bad)['status'],'BLOCKED_CLOCK')

    def test_bracket_exact_and_above(self):
        for width,expected in ((5000000,'PENDING_ENTRY'),(5000001,'BLOCKED_CLOCK')):
            self.assertEqual(self.confirm(sample(D-1000,0,width),sample(D-600,400000000))['status'],expected)
            self.assertEqual(self.confirm(sample(D-1000),sample(D-600,400000000,400000000+width))['status'],expected)

    def test_deadline_matrix(self):
        for a,c,status in ((-500,-400,'PENDING_ENTRY'),(-50,50,'MISSED_ACCEPTANCE_DEADLINE'),
             (-20,80,'MISSED_ACCEPTANCE_DEADLINE'),(-1,99,'MISSED_ACCEPTANCE_DEADLINE'),
             (-1,0,'PENDING_ENTRY'),(0,0,'PENDING_ENTRY'),(1,1,'MISSED_ACCEPTANCE_DEADLINE'),
             (-100,1,'MISSED_ACCEPTANCE_DEADLINE')):
            self.assertEqual(self.confirm(sample(D+a),sample(D+c,(c-a)*1000000))['status'],status)

    def test_mismatch_acceptance_fails(self):
        self.assertEqual(v3.confirmation(reservation(D-10),sample(D-11),sample(D-10,1000000))['status'],'BLOCKED_CLOCK')

    def test_profile_and_sampling_order(self):
        calls=[]
        with patch.object(v3,'verify_runtime') as verify,patch.object(v3.time,'perf_counter_ns',side_effect=lambda:(calls.append('p') or 1)),patch.object(v3.time,'time_ns',side_effect=lambda:(calls.append('u') or 1999999)):
            self.assertEqual(v3.sample(),sample(1,1));self.assertEqual(calls,['p','u','p']);verify.assert_called_once()

    def test_runtime_profile_fail_closed(self):
        u=dict(implementation='GetSystemTimeAsFileTime()',monotonic=False,adjustable=True,resolution=.015625)
        p=dict(implementation='QueryPerformanceCounter()',monotonic=True,adjustable=False,resolution=.0000001)
        with patch.object(v3.time,'get_clock_info',side_effect=[SimpleNamespace(**u),SimpleNamespace(**p)]):v3.verify_runtime()
        for which,key,val in ((0,'implementation','other'),(0,'resolution',.02),(1,'resolution',.000001),(1,'monotonic',False)):
            values=[u.copy(),p.copy()];values[which][key]=val
            with patch.object(v3.time,'get_clock_info',side_effect=[SimpleNamespace(**v) for v in values]),self.assertRaises(ValueError):v3.verify_runtime()

    def test_no_five_second_interval_or_rate_allowance(self):
        self.assertEqual(self.confirm(sample(D-10000),sample(D-4000,6000000000))['status'],'PENDING_ENTRY')
        self.assertNotIn('rate',v3.policy())

    def test_v2_incident_remains_blocked(self):
        from paper.reservations import AcceptanceStore
        r=reservation(1790870648261)
        c=AcceptanceStore._confirmation(r,1790870648645,390000000)
        self.assertEqual(c['status'],'BLOCKED_CLOCK')
        self.assertEqual(v3.replay_confirmation(r,c),c)
        self.assertEqual(v3.confirmation(r,sample(r['acceptance_ms']),sample(1790870648645,390000000))['status'],'PENDING_ENTRY')

    def test_deterministic_serialization_and_unknown_version(self):
        r=self.confirm(sample(D-1000),sample(D-600,400000000))
        self.assertEqual(encoded(r),encoded(copy.deepcopy(r)))
        with self.assertRaises(ValueError):v3.replay_confirmation(reservation(D-1000),dict(version='v4'))
