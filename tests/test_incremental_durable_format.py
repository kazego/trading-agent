"""Pure record tests only. No claim of filesystem or OS durability."""
import json
import unittest
from research.incremental_durable import decode_record
from research.incremental_ledger import Blocked
from research.spec import encoded, fingerprint


class FormatTests(unittest.TestCase):
    def record(self):
        value={'sequence':7,'state':{'head':'synthetic','equity':'1000'}}
        return dict(parent=None,value=value,value_hash=fingerprint(value))

    def test_exact_record(self):
        r=self.record();self.assertEqual(decode_record(encoded(r),None),r)

    def test_changed_state_rejected(self):
        r=self.record();r['value']['state']['equity']='7'
        with self.assertRaises(Blocked):decode_record(encoded(r),None)

    def test_wrong_parent_rejected(self):
        with self.assertRaises(Blocked):decode_record(encoded(self.record()),'wrong')

    def test_partial_record_rejected(self):
        with self.assertRaises(ValueError):decode_record(encoded(self.record())[:10],None)

    def test_extra_field_rejected(self):
        r=self.record();r['approval']=True
        with self.assertRaises(Blocked):decode_record(encoded(r),None)

    def test_noncanonical_record_rejected(self):
        with self.assertRaises(Blocked):decode_record(json.dumps(self.record(),indent=2).encode(),None)


if __name__=='__main__':unittest.main()
