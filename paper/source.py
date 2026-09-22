"""Read-only LIVE-0A evidence snapshots; no collector or execution dependency."""
from pathlib import Path
import json

from forward.manifest import load, implementation
from forward.operations import load_operations, verify_checkpoint
from forward.readout import connect, records
from paper.model import SourceIdentity, SourceObservation, parse
from research.spec import encoded, fingerprint


class SourceAdapter:
    """Expose immutable evidence only, never a connection or mutation operation."""

    def __init__(self, directory, expected):
        if type(expected) is not SourceIdentity:
            raise ValueError('Explicit source authorization required')
        self._directory = Path(directory).resolve(strict=True)
        self._expected = expected

    def snapshot(self):
        directory, expected = self._directory, self._expected
        # The unchanged production loader enforces historical/root/patch semantics.
        manifest = load(directory)
        operations = load_operations(directory, manifest)
        raw = (directory / 'operations-patch-v1.json').read_bytes()
        if not raw.endswith(b'\n'):
            raise ValueError('Noncanonical source patch')
        patch = parse(raw[:-1])
        if (manifest['manifest_fingerprint'] != expected.manifest
                or manifest['first_observation_ms'] != expected.first_observation_ms
                or operations['operations_fingerprint'] != expected.operations
                or operations['effective_boundary_ms'] != expected.operational_effective_ms
                or patch['patch_fingerprint'] != expected.patch
                or fingerprint({k: v for k, v in patch.items() if k != 'patch_fingerprint'}) != expected.patch
                or patch['operations_fingerprint'] != expected.operations
                or patch['semantic_contract'] != expected.semantic_contract
                or patch['new_implementation'] != expected.implementation
                or implementation() != expected.implementation):
            raise ValueError('Source authorization differs from explicit binding')
        if (directory / 'BLOCKED.json').exists():
            raise ValueError('Source is BLOCKED')
        with connect(directory, manifest) as db:
            verify_checkpoint(db, operations['checkpoint'])
            verify_checkpoint(db, patch['checkpoint'])
            if any(json.loads(r[0]).get('event') == 'BLOCKED'
                   for r in db.execute('SELECT payload FROM events')):
                raise ValueError('Source is BLOCKED')
            result = []
            for observation, _events in records(db, operations):
                receipt = observation.pop('persistence_receipt', None)
                observation.pop('effective_timing')
                result.append(SourceObservation(expected, encoded(observation),
                                                encoded(receipt) if receipt else None))
            return tuple(result)
