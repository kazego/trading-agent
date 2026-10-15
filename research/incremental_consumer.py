"""Synthetic adapters for the existing paper position loop.

No production loader, acceptance dispatch, snapshot or certificate.
Full replay at bootstrap; versioned reducer/local checks for each transition.
"""
from research.incremental_ledger import SyntheticLedger, ExternalMemory, Blocked
from paper.model import parse


class MemoryPositionLedger:
    def __init__(self, verifier, *, authorization_id, authorize, clock):
        self.verifier=verifier
        self.core=SyntheticLedger(verifier,ExternalMemory(),authorization_id=authorization_id,
                                  authorize=authorize,evidence=self._evidence,clock=clock)
        # The core rejects disk databases/attachments before any write.
        initial=verifier._verify()
        verifier._sources(initial)
        self.core.bootstrap()

    def _evidence(self,kind,data):
        if kind not in ('BAR','ENTRY'):raise Blocked('Position transition only')
        value=data['candle']
        if value is None:raise Blocked('Missing execution evidence')
        c=value['candle']
        actual=self.verifier.market.at(c['symbol'],c['open_time_ms'])
        if actual is None or actual.record!=value:raise Blocked('Execution evidence changed')

    def reconcile(self):
        self.core._validate_database()
        return self.core._state(self.core.recover())

    def process_candle(self,owner,stamp):
        state=self.reconcile();self.verifier._sources(state)
        old=self.core.db.execute('SELECT payload FROM journal WHERE request=?',
                                 ('BAR:'+owner+':'+str(stamp),)).fetchone()
        if old:
            data=parse(old[0].encode())['input']
            self._evidence('BAR',data)
            return self.core.apply('BAR',data)
        r=state.reservations[owner]['reservation']
        item=self.verifier.market.at(r['symbol'],stamp)
        if item is None:raise Blocked('Missing execution candle')
        return self.core.apply('BAR',dict(owner=owner,candle=item.record))

    def settle_entry(self,owner):
        state=self.reconcile();self.verifier._sources(state)
        r=state.reservations[owner]['reservation']
        item=self.verifier.market.at(r['symbol'],r['entry_boundary_ms'])
        if item is None:raise Blocked('Missing entry candle')
        return self.core.apply('ENTRY',dict(owner=owner,candle=item.record))


class DurablePositionLedger(MemoryPositionLedger):
    """Owned synthetic database only; caller retains ownership until DB close.

    Initialization is explicit and create-once. Reopen never bootstraps or renews
    audit validity. Historical verification precedes pending-publication recovery;
    normal transitions inherit the same local checks and consumer loop as memory.
    """
    def __init__(self, verifier, external, *, authorization_id, authorize, clock,
                 initialize=False):
        from research.incremental_durable import DurableLedger, DurableExternal
        if type(external) is not DurableExternal:
            raise Blocked('Owned durable external backend required')
        self.verifier=verifier
        self.core=DurableLedger(verifier,external,authorization_id=authorization_id,
                                authorize=authorize,evidence=self._evidence,clock=clock)
        # Ownership and database validation above must precede even read access.
        initial=verifier._verify()
        verifier._sources(initial)
        if initialize:
            self.core.bootstrap()
        else:
            self.core.recover()
