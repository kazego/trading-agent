"""Explicit DEVELOPMENT-only decision provider. No runner, writes or execution."""
from types import MappingProxyType

from backtest.clock import OpeningEvent
from backtest.data import DataValidationError, _hash
from research.data import SignalDataView, STEPS, HISTORY, authorize, VERSION as DATA_VERSION
from research.spec import SplitRequest, Role, fingerprint, policy, STEP_MS
from research.strategy import EMACrossover, EMAConfig, _issued_window, _execution_decision


class ResearchSchedule:
    def __init__(self, views, strategy):
        if not isinstance(strategy, EMACrossover) or strategy.config != EMAConfig():
            raise PermissionError("Only explicitly selected EMA 9/21 is currently approved")
        if not views:
            raise DataValidationError("Research data views required")
        checked = {}
        for symbol,view in views.items():
            if not isinstance(view, SignalDataView):
                raise DataValidationError("Use Phase 6B2 SignalDataView, not raw candles")
            meta = view.provenance
            request = SplitRequest(Role(meta["role"]),meta["start_ms"],meta["end_ms"],meta["warmup_start_ms"])
            authorize(request,meta["signal_timeframe"])
            step = STEPS[meta["signal_timeframe"]]
            if (meta["symbol"] != symbol or meta["aggregation_version"] != DATA_VERSION
                    or meta["policy_fingerprint"] != fingerprint(policy())
                    or meta["warmup_signal_candles"] != HISTORY
                    or meta["execution_resolution"] != "15m"
                    or len(view.candles) != HISTORY+(request.end_ms-request.start_ms)//step):
                raise DataValidationError("Invalid research view contract")
            for i,c in enumerate(view.candles):
                if (c.symbol != symbol or c.timeframe != meta["signal_timeframe"]
                        or c.open_time_ms != request.warmup_start_ms+i*step or c.close_time_ms != c.open_time_ms+step-1):
                    raise DataValidationError("Invalid research view chronology")
            if (_hash({k:v for k,v in meta.items() if k != "aggregate_fingerprint"}) != view.fingerprint
                    or _hash([c.record() for c in view.candles]) != meta["normalized_aggregate_fingerprint"]):
                raise DataValidationError("Research view fingerprint mismatch")
            checked[symbol] = SignalDataView(tuple(view.candles),view.provenance_json)
        if len({v.provenance["signal_timeframe"] for v in checked.values()}) != 1:
            raise DataValidationError("One signal timeframe per research provider")
        self._views = MappingProxyType(checked)
        self._strategy = strategy

    def window_at(self, symbol, timestamp):
        if symbol not in self._views:
            raise DataValidationError("Symbol not supplied to research schedule")
        view = self._views[symbol]
        meta = view.provenance
        if type(timestamp) is not int or not meta["start_ms"] <= timestamp < meta["end_ms"] or timestamp % STEP_MS:
            raise PermissionError("Outside authorized DEVELOPMENT execution boundaries")
        if timestamp % STEPS[meta["signal_timeframe"]]:
            return None
        return _issued_window(view.at_open(timestamp))

    def evaluate(self, symbol, timestamp):
        window = self.window_at(symbol,timestamp)
        return None if window is None else self._strategy.evaluate(window)

    def __call__(self, event: OpeningEvent):
        """Narrow Phase 5E bridge, supplied explicitly as decision_provider."""
        if not isinstance(event, OpeningEvent) or event.timeframe != "15m" or event.symbol not in self._views:
            raise DataValidationError("Matching 15m execution opening required")
        view = self._views[event.symbol]
        if event.dataset_fingerprint != view.provenance["source_15m_dataset_fingerprint"]:
            raise DataValidationError("Execution dataset differs from the signal view source")
        window = self.window_at(event.symbol,event.open_time_ms)
        if window is None:
            return None
        if event.opening_price != window.opening_price or event.completed_through_ms != window.completed_through_ms:
            raise DataValidationError("Signal and execution opening disagree")
        return _execution_decision(self._strategy.evaluate(window),event)
