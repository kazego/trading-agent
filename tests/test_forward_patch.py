"""Offline patch-chain tests: synthetic evidence only, no registration or HTTP."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtest.data import Candle, SYMBOLS
from forward import manifest, operations as op
from forward.model import LABEL, FOUR_HOURS
from forward.store import Store
from research.spec import encoded, fingerprint


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)
        self.first=1789171200000
        self.meta=dict(manifest_fingerprint='synthetic',history_start_ms=self.first-900000,
                       first_observation_ms=self.first)
        self.store=Store(self.path,self.meta)
        for symbol in SYMBOLS:
            cs=[Candle(symbol,'15m',self.first-900000+i*900000,self.first+i*900000-1,
                       '100','101','99','100','1') for i in range(17)]
            self.store.append(symbol,cs,received_ms=self.first+2*FOUR_HOURS)
        for i,timing in enumerate(['ONLINE','DELAYED_RECONSTRUCTED']):
            self.store.observation(dict(label=LABEL,boundary_ms=self.first+i*FOUR_HOURS,
                manifest_fingerprint='synthetic',timing=timing))
        self.root=dict(label=LABEL,version='operations-v1',manifest_fingerprint='synthetic',
            implementation=op.PATCH_OLD,effective_boundary_ms=self.first+2*FOUR_HOURS,
            policy=op.policy(),policy_fingerprint=fingerprint(op.policy()),
            checkpoint=op.checkpoint(self.store.db))
        self.root['operations_fingerprint']=fingerprint(self.root)
        self.path.joinpath('operations-v1.json').write_bytes(encoded(self.root)+b'\n')
        self.root_mock=patch.object(op,'PATCH_ROOT',self.root['operations_fingerprint']);self.root_mock.start()
        self.manifest_mock=patch.object(manifest,'load',return_value=self.meta);self.manifest_mock.start()
        self.effective=self.first+3*FOUR_HOURS

    def tearDown(self):
        self.manifest_mock.stop();self.root_mock.stop();self.store.close();self.temp.cleanup()

    def proposal(self): return op.plan_patch(self.path,effective_ms=self.effective)

    def bind(self):
        r=self.proposal();self.store.close()
        return op.bind_patch(self.path,effective_ms=self.effective,
            approved_implementation=r['new_implementation'],approved_patch=r['patch_fingerprint'])

    def mutate(self,key,value):
        p=self.path/'operations-patch-v1.json';r=json.loads(p.read_bytes());r[key]=value
        r['patch_fingerprint']=fingerprint({k:v for k,v in r.items() if k!='patch_fingerprint'})
        p.write_bytes(encoded(r)+b'\n')

    def test_valid_chain_deterministic_preserves_root_and_evidence(self):
        before=self.path.joinpath('operations-v1.json').read_bytes()
        rows=self.store.db.execute('SELECT * FROM observations ORDER BY boundary_ms').fetchall()
        self.assertEqual(self.proposal(),self.proposal())
        self.assertFalse((self.path/'operations-patch-v1.json').exists())
        self.bind()
        self.assertEqual(op.load_operations(self.path,self.meta),self.root)
        self.assertEqual(before,self.path.joinpath('operations-v1.json').read_bytes())
        from forward.readout import connect
        with connect(self.path,self.meta) as db:
            self.assertEqual(rows,db.execute('SELECT * FROM observations ORDER BY boundary_ms').fetchall())

    def test_missing_patch_fails(self):
        with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_noncanonical_or_tampered_patch_fails(self):
        self.bind();p=self.path/'operations-patch-v1.json';p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_rehashed_wrong_identity_ancestry_scope_and_policy_fail(self):
        self.bind();p=self.path/'operations-patch-v1.json';original=p.read_bytes()
        for key in ['old_implementation','new_implementation','operations_fingerprint',
                    'reviewed_encoding_implementation','scope','authorization_scope',
                    'semantic_contract','policy_fingerprint','original_effective_boundary_ms']:
            with self.subTest(key=key):
                p.write_bytes(original);self.mutate(key,'wrong')
                with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_unknown_runtime_fails(self):
        self.bind()
        with patch.object(manifest,'implementation',return_value='unknown'):
            with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_checkpoint_mismatch_fails_even_when_rehashed(self):
        self.bind();r=json.loads((self.path/'operations-patch-v1.json').read_bytes())
        r['checkpoint']['events']['fingerprint']='0'*64
        self.mutate('checkpoint',r['checkpoint'])
        with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_checkpoint_includes_evidence_appended_since_root(self):
        self.store.event('START')
        r=self.proposal()
        self.assertEqual(r['checkpoint']['events']['count'],self.root['checkpoint']['events']['count']+1)
        self.assertNotEqual(r['checkpoint'],self.root['checkpoint'])

    def test_explicit_approval_exclusive_write_and_checkpoint_race(self):
        r=self.proposal();self.store.event('STOP');self.store.close()
        with self.assertRaises(ValueError):
            op.bind_patch(self.path,effective_ms=self.effective,
                approved_implementation=r['new_implementation'],approved_patch=r['patch_fingerprint'])
        self.assertFalse((self.path/'operations-patch-v1.json').exists())
        self.bind()
        with self.assertRaises(FileExistsError): self.bind()

    def test_not_effective_yet_fails_without_mutating_evidence(self):
        self.bind()
        with patch('time.time_ns',return_value=(self.effective-1)*1000000):
            with self.assertRaises(ValueError): op.load_operations(self.path,self.meta)

    def test_out_of_scope_source_changes_fail(self):
        with patch.object(op,'PATCH_REVIEWED','0'*64):
            with self.assertRaises(ValueError): self.proposal()

    def test_negative_or_boolean_effective_time_rejected(self):
        for value in [-1,True,self.first-1]:
            with self.assertRaises(ValueError): op.plan_patch(self.path,effective_ms=value)
