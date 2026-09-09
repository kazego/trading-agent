"""Append-only local research ledger. No runner, data loader or network access."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

from research.spec import (ExperimentSpec, Hypothesis, SplitRequest, Role, HOLDOUT_START,
                           SYMBOLS, STEP_MS, digest, nonempty, encoded, fingerprint)


class Registry:
    """Single-writer exclusive lock; torn writes/corruption stop future operations.

    A stale lock is never removed automatically. This is an accidental-access
    control, not a security boundary against an operator editing local files.
    """
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "events.jsonl"

    def events(self):
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("Incomplete ledger tail; manual recovery required")
        events = []
        for line in raw.splitlines():
            event = json.loads(line)
            body = {k: v for k, v in event.items() if k != "event_id"}
            if (event["sequence"] != len(events) + 1 or event["previous"] != (events[-1]["event_id"] if events else None)
                    or fingerprint(body) != event["event_id"] or encoded(event) != line):
                raise ValueError("Ledger integrity failure")
            events.append(event)
        return events

    @contextmanager
    def _writer(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / "writer.lock"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            yield self.events()
        finally:
            os.close(fd)
            lock.unlink()

    def _append(self, events, kind, payload):
        body = dict(sequence=len(events)+1, previous=events[-1]["event_id"] if events else None,
                    kind=kind, payload=payload)
        event = dict(body, event_id=fingerprint(body))
        with self.path.open("ab") as stream:
            stream.write(encoded(event)+b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event["event_id"]

    @staticmethod
    def _find(events, kind, key, value):
        return [e for e in events if e["kind"] == kind and e["payload"].get(key) == value]

    @classmethod
    def _experiment(cls, events, experiment_id):
        found = cls._find(events, "EXPERIMENT", "experiment_id", experiment_id)
        if not found:
            raise ValueError("Unknown experiment")
        return found[0]["payload"]["spec"]

    def register_hypothesis(self, hypothesis):
        if not isinstance(hypothesis, Hypothesis):
            raise ValueError("Hypothesis required")
        with self._writer() as events:
            for parent in hypothesis.parents:
                self._experiment(events, parent)
            if not self._find(events, "HYPOTHESIS", "hypothesis_id", hypothesis.id):
                self._append(events, "HYPOTHESIS", dict(hypothesis_id=hypothesis.id, record=hypothesis.record))
            return hypothesis.id

    def register_experiment(self, spec, hypothesis_id):
        if not isinstance(spec, ExperimentSpec):
            raise ValueError("ExperimentSpec required")
        with self._writer() as events:
            if not self._find(events, "HYPOTHESIS", "hypothesis_id", hypothesis_id):
                raise ValueError("Register hypothesis first")
            existing = self._find(events, "EXPERIMENT", "experiment_id", spec.id)
            if existing:
                self._append(events, "DUPLICATE", dict(experiment_id=spec.id, hypothesis_id=hypothesis_id))
                return spec.id, False
            # Exactly one executable identity per approved timeframe; code revisions
            # are additional configurations and need a future budget-policy change.
            if any(e["kind"] == "EXPERIMENT" and e["payload"]["spec"]["signal_timeframe"] == spec.signal_timeframe for e in events):
                raise ValueError("Approved timeframe slot already registered")
            self._append(events, "EXPERIMENT", dict(experiment_id=spec.id, hypothesis_id=hypothesis_id, spec=spec.record))
            return spec.id, True

    def record_rejection(self, spec, hypothesis_id, reason):
        """Preserve rejected proposals without treating them as completed evidence."""
        nonempty(reason)
        with self._writer() as events:
            if not self._find(events, "HYPOTHESIS", "hypothesis_id", hypothesis_id):
                raise ValueError("Unknown hypothesis")
            return self._append(events, "REJECTED_PROPOSAL", dict(experiment_id=spec.id, spec=spec.record,
                hypothesis_id=hypothesis_id, reason=reason))

    @classmethod
    def _completed(cls, events, experiment_id, role):
        starts = {e["event_id"] for e in events if e["kind"] == "ATTEMPT" and
                  e["payload"]["experiment_id"] == experiment_id and e["payload"]["split"]["role"] == role.value}
        return [e for e in events if e["kind"] == "OUTCOME" and e["payload"]["attempt_id"] in starts
                and e["payload"]["status"] == "COMPLETED"]

    def promote_validation(self, experiment_id, operator, rationale):
        nonempty(operator); nonempty(rationale)
        with self._writer() as events:
            self._experiment(events, experiment_id)
            evidence = self._completed(events, experiment_id, Role.DEVELOPMENT)
            if not evidence:
                raise ValueError("Completed development evidence required")
            if self._find(events, "VALIDATION_PROMOTION", "experiment_id", experiment_id):
                raise ValueError("Promotion already frozen")
            return self._append(events, "VALIDATION_PROMOTION", dict(experiment_id=experiment_id,
                operator=operator, rationale=rationale, evidence_event=evidence[-1]["event_id"]))

    def authorize_holdout(self, experiment_id, endpoint_ms, operator, rationale):
        nonempty(operator); nonempty(rationale)
        SplitRequest(Role.HOLDOUT, HOLDOUT_START, endpoint_ms, HOLDOUT_START)
        with self._writer() as events:
            self._experiment(events, experiment_id)
            evidence = self._completed(events, experiment_id, Role.VALIDATION)
            if not evidence:
                raise ValueError("Completed validation plus separate operator authorization required")
            prior = [e for e in events if e["kind"] == "HOLDOUT_AUTHORIZATION"]
            if any(e["payload"]["endpoint_ms"] != endpoint_ms for e in prior):
                raise ValueError("Global holdout endpoint already frozen")
            if any(e["payload"]["experiment_id"] == experiment_id for e in prior):
                raise ValueError("Holdout authorization already frozen")
            return self._append(events, "HOLDOUT_AUTHORIZATION", dict(experiment_id=experiment_id,
                endpoint_ms=endpoint_ms, operator=operator, rationale=rationale, evidence_event=evidence[-1]["event_id"]))

    @classmethod
    def _check_access(cls, events, experiment_id, request):
        if not isinstance(request, SplitRequest):
            raise ValueError("Typed split required")
        spec = cls._experiment(events, experiment_id)
        signal_step = {"15m": STEP_MS, "1h": STEP_MS*4, "4h": STEP_MS*16}[spec["signal_timeframe"]]
        if request.warmup_start_ms != request.start_ms - 99*signal_step or request.end_ms % signal_step:
            raise ValueError("Exact history-only warmup and signal-aligned endpoint required")
        if request.role == Role.VALIDATION and not cls._find(events, "VALIDATION_PROMOTION", "experiment_id", experiment_id):
            raise PermissionError("Validation is locked until explicit promotion")
        if request.role == Role.HOLDOUT:
            grants = cls._find(events, "HOLDOUT_AUTHORIZATION", "experiment_id", experiment_id)
            if not grants or grants[0]["payload"]["endpoint_ms"] != request.end_ms:
                raise PermissionError("Holdout unauthorized or endpoint differs")

    def record_access(self, experiment_id, request, purpose, operator):
        """Future loaders must call this BEFORE acquisition/access, never after it."""
        if purpose not in ("ACQUISITION", "READ"):
            raise ValueError("Unknown access purpose")
        nonempty(operator)
        with self._writer() as events:
            self._check_access(events, experiment_id, request)
            return self._append(events, "ACCESS", dict(experiment_id=experiment_id, split=request.record,
                purpose=purpose, operator=operator))

    def begin_attempt(self, experiment_id, request, dataset_fingerprints):
        """Records metadata only. Does not execute experiments or read datasets."""
        pairs = dict(dataset_fingerprints)
        if set(pairs) != set(SYMBOLS):
            raise ValueError("All configured dataset fingerprints required")
        for value in pairs.values(): digest(value)
        with self._writer() as events:
            self._check_access(events, experiment_id, request)
            spec = self._experiment(events, experiment_id)
            if request.role == Role.DEVELOPMENT and pairs != dict(spec["development_dataset_fingerprints"]):
                raise ValueError("Development datasets differ from frozen specification")
            if not any(e["kind"] == "ACCESS" and e["payload"]["experiment_id"] == experiment_id
                       and e["payload"]["split"] == request.record for e in events):
                raise PermissionError("Record authorized access before an attempt")
            run_id = fingerprint(dict(experiment_id=experiment_id, split=request.record, datasets=pairs))
            previous = self._find(events, "ATTEMPT", "run_id", run_id)
            if previous:
                outcomes = self._find(events, "OUTCOME", "attempt_id", previous[-1]["event_id"])
                if not outcomes or outcomes[-1]["payload"]["status"] != "FAILED":
                    raise ValueError("Only failed attempts may retry; completed runs are immutable")
            # A split cannot be rerun against substituted data under the same experiment.
            if any(e["kind"] == "ATTEMPT" and e["payload"]["experiment_id"] == experiment_id and
                   e["payload"]["split"]["role"] == request.role.value and e["payload"]["run_id"] != run_id for e in events):
                raise ValueError("Split inputs already frozen")
            return self._append(events, "ATTEMPT", dict(experiment_id=experiment_id, run_id=run_id,
                split=request.record, datasets=pairs, attempt_number=len(previous)+1))

    def finish_attempt(self, attempt_id, status, result_fingerprint=None, error=None):
        if status not in ("COMPLETED", "FAILED"):
            raise ValueError("Explicit terminal status required")
        if status == "COMPLETED":
            digest(result_fingerprint)
            if error is not None: raise ValueError("Completed attempt cannot carry an error")
        else:
            nonempty(error)
            if result_fingerprint is not None: raise ValueError("Failed attempt cannot carry completed result")
        with self._writer() as events:
            if not any(e["event_id"] == attempt_id and e["kind"] == "ATTEMPT" for e in events):
                raise ValueError("Unknown attempt")
            if self._find(events, "OUTCOME", "attempt_id", attempt_id):
                raise ValueError("Outcome already recorded")
            return self._append(events, "OUTCOME", dict(attempt_id=attempt_id, status=status,
                result_fingerprint=result_fingerprint, error=error))

    def record_disclosure(self, result_fingerprint, operator, kind, rationale):
        digest(result_fingerprint); nonempty(operator); nonempty(rationale)
        if kind not in ("VIEWED", "DISCLOSED", "PRIOR_EXPOSURE"):
            raise ValueError("Unknown disclosure kind")
        with self._writer() as events:
            if kind != "PRIOR_EXPOSURE" and not any(e["kind"] == "OUTCOME" and
                    e["payload"]["result_fingerprint"] == result_fingerprint for e in events):
                raise ValueError("Unknown completed result")
            return self._append(events, kind, dict(result_fingerprint=result_fingerprint,
                operator=operator, rationale=rationale))
