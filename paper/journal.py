"""Deterministic B4 journal reducer. No I/O, clock, activation or reporting."""
from dataclasses import asdict
from decimal import Decimal as D
import hashlib
from pathlib import Path

from backtest.data import Candle, canonical_decimal
from backtest.simulation import arithmetic
from paper.acceptance import BoundaryBatch, decide, timestamp
from paper.market import CandleEvidence
from paper.model import LABEL, SourceObservation, parse
from paper.reservations import AcceptanceStore
from paper.settlement import ConfirmedEntry, settle, protect, implementation as b3_identity
from research.spec import canonical, encoded, fingerprint


HISTORICAL = {
    'commit': '9f33ecfdbf604bb5425995f2235f52f6263ebb14',
    'b1': 'f3f5e761bcbda57ff75dfb2d514bfc17cd8a54ff57dc79e7e36e5bd4e8aab353',
    'b2': '3a51e20e00f3f6faaf3b7cb3f2e575996accc85b978657bbdc43979a4712f8b1',
    'b3': '75502f8a4509239a6fb0d34bd4a54226cac561a5868e90007d34f62d680e9170',
}


def implementation():
    root = Path(__file__).resolve().parents[1]
    return fingerprint(dict(version='paper-b4-v1', historical=HISTORICAL, settlement=b3_identity(),
        sources={p: hashlib.sha256((root/p).read_text(encoding='utf-8').encode()).hexdigest()
                 for p in ('paper/journal.py', 'paper/ledger.py', 'paper/clock_v3.py', 'paper/recovery.py')}))


def observation(source, value):
    item = SourceObservation(source, encoded(value['observation']),
                             encoded(value['receipt']) if value['receipt'] else None)
    if item.record != value:
        raise ValueError('Observation provenance changed')
    return item


def candle(source, value):
    item = CandleEvidence(source, Candle(**value['candle']), value['row_digest'], value['received_ms'])
    if item.record != value:
        raise ValueError('Candle provenance changed')
    return item


class TemporalConsistencyError(ValueError):
    """Claimed time precedes durable state; carry safe audit metadata only."""
    def __init__(self, operation, claimed_ms, financial_ms, state_ms):
        super().__init__('Claimed time precedes incorporated evidence/state')
        self.audit = dict(operation=operation, claimed_ms=claimed_ms,
                          financial_available_at_ms=financial_ms, state_available_at_ms=state_ms)


def source_available(value):
    stamps = [timestamp(value['observation']['observed_ms'])]
    if value['receipt'] is not None:
        stamps.append(timestamp(value['receipt']['observed_ms']))
    return max(stamps)


def latest(*stamps):
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


class Replay:
    """Rebuild exclusively from already-durable acceptance and transition events."""
    def __init__(self, binding, rules, identity):
        self.binding, self.rules, self.identity = binding, rules, identity
        self.batches, self.confirmations, self.reservations = {}, {}, {}
        self.positions, self.trades, self.processed, self.candles = {}, {}, {}, {}
        self.accounting = []
        self.resolutions, self.event_ids = {}, {}
        self.journal_head = identity
        self.slot = ('EMPTY', None, None)
        self.equity = D('1000')
        self.accounting_head = identity
        # No wall clock or invented T0: only a future explicit activation binding
        # can make the initial capital available. Tests supply synthetic gates.
        self.financial_available_ms = binding.prospective.start_ms
        self.state_available_ms = binding.prospective.start_ms

    def _not_before(self, operation, claimed_ms):
        timestamp(claimed_ms)
        floor = latest(self.financial_available_ms, self.state_available_ms)
        if floor is not None and claimed_ms < floor:
            raise TemporalConsistencyError(operation, claimed_ms,
                                           self.financial_available_ms, self.state_available_ms)

    @property
    def basis(self):
        return canonical(dict(label=LABEL, basis='B4_REALIZED_CLOSED_TRADE_BALANCE',
            draft_id=self.binding.draft.id, amount=self.equity, currency='USDT',
            accounting_head=self.accounting_head))

    def result(self, boundary):
        batch = self.batches[boundary]
        result = parse(encoded(batch['decision']))
        r = result['reservation']
        result.update(confirmation=None, reservation_state=None,
                      effective_dispositions=parse(encoded(result['dispositions'])))
        if r:
            confirmation = self.confirmations.get(r['reservation_id'])
            state = ('ABANDONED' if r['reservation_id'] in self.resolutions else
                     confirmation['status'] if confirmation else 'UNCONFIRMED_ACCEPTANCE')
            result.update(confirmation=confirmation, reservation_state=state)
            for d in result['effective_dispositions']:
                if d['symbol'] == r['symbol']:
                    d['reason'] = state
        return result

    def instruction(self, owner):
        r = self.reservations[owner]['reservation']
        batch = self.batches[r['signal_boundary_ms']]
        return ConfirmedEntry(self.binding, observation(self.binding.draft.source, batch['source']),
                              encoded(self.result(r['signal_boundary_ms'])), encoded(batch['equity']))

    @arithmetic
    def apply(self, kind, data):
        required = {'ACCEPT': {'source', 'accepted_ms', 'equity'},
                    'CONFIRM': {'owner', 'durable_ms', 'elapsed_ns'},
                    'CONFIRM_V3': {'owner','acceptance','durable','authorization','approval'},
                    'ABANDON_BLOCKED_CLOCK': {'owner','authorization','approval'},
                    'ENTRY': {'owner', 'candle'}, 'BAR': {'owner', 'candle'}}
        if kind not in required or type(data) is not dict or set(data) != required[kind]:
            raise ValueError('Malformed journal input')
        if kind == 'ACCEPT':
            self._not_before(kind, data['accepted_ms'])
            item = observation(self.binding.draft.source, data['source'])
            boundary = item.boundary_ms
            if boundary in self.batches or self.equity <= 0:
                raise ValueError('Duplicate acceptance or insolvent account')
            if self.batches:
                last = self.batches[max(self.batches)]['decision']
                if boundary <= max(self.batches) or data['accepted_ms'] < last['acceptance_ms']:
                    raise ValueError('Nonchronological acceptance')
            if data['equity'] != self.basis:
                raise ValueError('Stale committed equity')
            decision = decide(self.binding, BoundaryBatch.from_evidence(item),
                accepted_ms=data['accepted_ms'], slot=self.slot[0], equity_basis=encoded(data['equity']))
            self.batches[boundary] = dict(source=data['source'], equity=data['equity'], decision=decision)
            r = decision['reservation']
            if r:
                if self.financial_available_ms is None:
                    raise ValueError('Initial equity availability requires activation binding')
                owner = r['reservation_id']
                self.reservations[owner] = dict(reservation=r, state='UNCONFIRMED_ACCEPTANCE')
                self.slot = ('PENDING', owner, r['symbol'])
            self.state_available_ms = latest(self.state_available_ms, data['accepted_ms'], source_available(data['source']))
            return self.result(boundary)
        owner = data['owner']
        r = self.reservations[owner]
        if kind == 'ABANDON_BLOCKED_CLOCK':
            from paper.recovery import apply
            return apply(self,owner,data['authorization'],data['approval'])
        if kind in ('CONFIRM','CONFIRM_V3'):
            if owner in self.confirmations or self.slot[1] != owner:
                raise ValueError('Duplicate or unowned confirmation')
            if kind == 'CONFIRM':
                confirmation = AcceptanceStore._confirmation(r['reservation'], data['durable_ms'], data['elapsed_ns'])
            else:
                from paper.clock_v3 import confirmation as confirm_v3, ClockAuthorization
                auth = ClockAuthorization(encoded(data['authorization']),data['approval'])
                auth.check(self.binding,r['reservation']['acceptance_ms'])
                confirmation = confirm_v3(r['reservation'],data['acceptance'],data['durable'])
            if confirmation['status'] == 'PENDING_ENTRY':
                self._not_before(kind, confirmation['durable_ms'])
            self.confirmations[owner] = confirmation
            self.state_available_ms = latest(self.state_available_ms, confirmation['durable_ms'])
            r['state'] = confirmation['status']
            if r['state'] != 'PENDING_ENTRY':
                self.slot = ('BLOCKED', owner, r['reservation']['symbol'])
            return self.result(r['reservation']['signal_boundary_ms'])
        if self.slot[1] != owner or self.slot[0] not in ('PENDING', 'OPEN'):
            raise ValueError('No executable exposure owns global slot')
        item = candle(self.binding.draft.source, data['candle']) if data['candle'] is not None else None
        if kind == 'ENTRY':
            if r['state'] != 'PENDING_ENTRY':
                raise ValueError('Confirmed PENDING_ENTRY required')
            transition = settle(self.instruction(owner), item, self.rules)
        elif kind == 'BAR':
            if r['state'] != 'OPEN':
                raise ValueError('OPEN position required')
            transition = protect(self.positions[owner], item)
        else:
            raise ValueError('Unknown journal event')
        if transition.status == 'UNRESOLVED':
            if item is not None:
                raise ValueError('Inconsistent source evidence; exposure must remain blocked')
            return transition.record
        if item is None:
            raise ValueError('Missing evidence for financial transition')
        old = self.candles.get(item.id)
        if old is not None and old != item.record:
            raise ValueError('Conflicting evidence')
        self.candles[item.id] = item.record
        self.state_available_ms = latest(self.state_available_ms, item.received_ms)
        if kind == 'BAR':
            key = (owner, item.candle.open_time_ms)
            if key in self.processed:
                raise ValueError('Duplicate processed candle')
            self.processed[key] = item.id
        r['state'] = transition.status
        if transition.position is not None:
            self.positions[owner] = transition.position
            self.slot = ('OPEN', owner, r['reservation']['symbol'])
        elif transition.trade is not None:
            trade = transition.trade
            if owner in self.trades:
                raise ValueError('Duplicate closed trade')
            self.trades[owner] = trade
            batch = self.batches[r['reservation']['signal_boundary_ms']]
            evidence_available_ms = max(
                source_available(batch['source']), self.confirmations[owner]['durable_ms'],
                trade.position.entry_evidence.received_ms, item.received_ms,
                *(self.candles[key]['received_ms'] for (position_owner, _), key in self.processed.items()
                  if position_owner == owner))
            financial_available_ms = latest(self.financial_available_ms, evidence_available_ms)
            event = canonical(dict(label=LABEL, version='paper-equity-v1', owner=owner, trade=trade.id,
                previous=self.accounting_head, equity_before=self.equity, net_pnl=trade.net_pnl,
                equity_after=self.equity+trade.net_pnl,
                financial_available_before_ms=self.financial_available_ms,
                source_evidence_available_at_ms=evidence_available_ms,
                financial_available_at_ms=financial_available_ms,
                exit_evidence=item.id, exit_evidence_received_ms=item.received_ms))
            event = dict(event, event_id=fingerprint(event))
            self.accounting.append(event)
            self.equity = D(event['equity_after'])
            self.accounting_head = event['event_id']
            self.financial_available_ms = financial_available_ms
            self.slot = ('EMPTY', None, None)
        else:
            self.slot = ('EMPTY', None, None)  # Definitive rejection only.
        return transition.record

    def rows(self):
        """Exact materialized rows expected from the authoritative journal."""
        dump = lambda value: encoded(value).decode()
        fills = []
        for owner, position in self.positions.items():
            value = canonical(asdict(position.entry))
            fills.append((owner, 'ENTRY', fingerprint(dict(owner=owner, leg='ENTRY', fill=value)), dump(value)))
        for owner, trade in self.trades.items():
            value = canonical(asdict(trade.exit))
            fills.append((owner, 'EXIT', fingerprint(dict(owner=owner, leg='EXIT', fill=value)), dump(value)))
        return dict(
            resolutions=[(owner,fingerprint(value),dump(value)) for owner,value in self.resolutions.items()],
            reservations=[(owner, r['reservation']['signal_boundary_ms'], r['reservation']['symbol'],
                           r['state'], dump(r['reservation'])) for owner, r in self.reservations.items()],
            positions=[(owner, p.id, dump(p.record)) for owner, p in self.positions.items()],
            fills=fills,
            candles=[(key, dump(value)) for key, value in self.candles.items()],
            processed=[(owner, stamp, key) for (owner, stamp), key in self.processed.items()],
            trades=[(owner, t.id, dump(t.record)) for owner, t in self.trades.items()],
            equity=[(i+1, e['owner'], e['event_id'], e['equity_before'], e['net_pnl'],
                     e['equity_after'], e['financial_available_at_ms'], dump(e)) for i, e in enumerate(self.accounting)],
            global_slot=[(1, *self.slot)],
            account=[(1, canonical_decimal(self.equity), self.accounting_head,
                      self.financial_available_ms, self.state_available_ms)])
