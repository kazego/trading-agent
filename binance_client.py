"""Read-only USDT-M Demo adapter. No exchange mutations or retry loops.

The legacy bot loop is not wired to this normalized API. Do not run bot.py as
an execution entry point until a later approved execution integration exists.
"""

import re
import time
from decimal import Decimal, InvalidOperation

from binance.client import Client
from binance.exceptions import BinanceAPIException
from requests.exceptions import Timeout, ConnectionError as TransportConnectionError

import config
from errors import (AccountModeError, AuthenticationError, ConfigurationError,
                    RateLimitError, ReadTimeoutError, TransientReadError,
                    UnknownExchangeStateError)
from models import (ExchangeSnapshot, SymbolFilters, ExchangeInfoSnapshot,
                    BalanceSnapshot, BalancesSnapshot, AccountSnapshot,
                    PositionSnapshot, PositionsSnapshot, OrdinaryOrderSnapshot,
                    AlgoOrderSnapshot, OrdinaryOrdersSnapshot, AlgoOrdersSnapshot,
                    FillSnapshot, FillsSnapshot)

DEMO_BASE = "https://demo-fapi.binance.com/fapi"
SIDES = {"BUY", "SELL"}
POSITION_SIDES = {"BOTH", "LONG", "SHORT"}
WORKING_TYPES = {"MARK_PRICE", "CONTRACT_PRICE"}
ORDER_TYPES = {"LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT",
               "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
ORDER_STATUSES = {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED",
                  "EXPIRED", "EXPIRED_IN_MATCH"}
ALGO_STATUSES = {"NEW", "CANCELED", "TRIGGERING", "TRIGGERED", "FINISHED", "REJECTED", "EXPIRED"}
SYMBOL_STATUSES = {"PENDING_TRADING", "TRADING", "PRE_DELIVERING", "DELIVERING", "DELIVERED",
                   "PRE_SETTLE", "SETTLING", "CLOSE", "TRADING_HALT", "TRADING_CANCEL_ONLY"}


def _bad():
    raise UnknownExchangeStateError("Malformed or unsupported Demo response")


def _text(value):
    if not isinstance(value, str) or not value.strip():
        _bad()
    return value


def _enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        _bad()
    return value


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        _bad()
    return value


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return value
    _bad()


def _bool(value):
    if type(value) is bool:
        return value
    if value in ("true", "false") and type(value) is str:
        return value == "true"
    _bad()


def _decimal(value, *, signed=False):
    # JSON financial fields must be strings; never round a transport float.
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        _bad()
    try:
        result = Decimal(value)
    except InvalidOperation:
        _bad()
    if not result.is_finite() or (not signed and result < 0):
        _bad()
    return result


def _optional_decimal(row, field, *, signed=False):
    return None if field not in row or row[field] is None else _decimal(row[field], signed=signed)


def _rows(raw):
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        _bad()
    return raw


def _unique(items, key):
    identities = [key(item) for item in items]
    if len(set(identities)) != len(identities):
        _bad()
    return tuple(items)


def _translate(error):
    # Never stringify SDK exceptions, response bodies, URLs, or credentials.
    if isinstance(error, (Timeout, TimeoutError)):
        return ReadTimeoutError("Demo read timed out")
    if isinstance(error, TransportConnectionError):
        return TransientReadError("Demo connection failed")
    if isinstance(error, BinanceAPIException):
        code, status = error.code, error.status_code
        if code in (-2014, -2015, -1022) or status in (401, 403):
            return AuthenticationError("Demo authentication or access denied")
        if code == -1003 or status in (418, 429):
            return RateLimitError("Demo read rate limited")
        if code == -1007:
            return ReadTimeoutError("Demo service read timed out")
        if code in (-1001, -1008) or (type(status) is int and 500 <= status <= 599):
            return TransientReadError("Demo service read failed")
    return UnknownExchangeStateError("Unrecognized Demo read failure")


class BinanceFuturesClient:
    def __init__(self, *, api_key=None, api_secret=None, sdk_factory=None):
        if config.USE_TESTNET is not True:
            raise ConfigurationError("Only the approved Futures Demo environment is allowed")
        if type(config.API_TIMEOUT_SECONDS) is not int or config.API_TIMEOUT_SECONDS <= 0:
            raise ConfigurationError("A positive read timeout is required")
        key = config.API_KEY if api_key is None else api_key
        secret = config.API_SECRET if api_secret is None else api_secret
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
            raise ConfigurationError("Explicit Futures Demo credentials are required")
        factory = Client if sdk_factory is None else sdk_factory
        try:
            self._client = factory(key, secret, demo=True, testnet=False, ping=False,
                                   verbose=False, requests_params={
                                       "timeout": config.API_TIMEOUT_SECONDS,
                                       "allow_redirects": False,
                                   })
        except Exception as error:
            raise _translate(error) from None
        self._validate_environment()

    def _validate_environment(self):
        try:
            valid = self._client.demo is True and self._client.testnet is False
            for version in (1, 2, 3):
                valid = valid and self._client._create_futures_api_uri("time", version) == (
                    DEMO_BASE + f"/v{version}/time")
        except Exception:
            valid = False
        if not valid:
            raise ConfigurationError("Resolved Futures endpoint is not the approved Demo endpoint")

    def _read(self, method, source, parser, **params):
        self._validate_environment()  # Also detect accidental SDK flag/URL changes after initialization.
        started = time.time_ns() // 1_000_000
        try:
            raw = getattr(self._client, method)(**params)
        except Exception as error:
            raise _translate(error) from None
        metadata = ExchangeSnapshot(source, started, time.time_ns() // 1_000_000)
        try:
            return parser(raw, metadata)
        except (AccountModeError, UnknownExchangeStateError):
            raise
        except Exception:
            raise UnknownExchangeStateError("Malformed or unsupported Demo response") from None

    @staticmethod
    def _symbol(symbol):
        if symbol not in config.FIXED_SYMBOLS:
            raise ConfigurationError("Requested symbol is outside the fixed watchlist")
        return symbol

    def get_server_time(self) -> int:
        return self._read("futures_time", "/fapi/v1/time",
                          lambda raw, meta: _integer(raw["serverTime"]))

    def get_exchange_info(self) -> ExchangeInfoSnapshot:
        def parse(raw, meta):
            rows = _rows(raw["symbols"])
            _unique(rows, lambda row: _text(row["symbol"]))
            by_symbol = {row["symbol"]: row for row in rows}
            return ExchangeInfoSnapshot(meta, tuple(self._filters(by_symbol[s]) for s in config.FIXED_SYMBOLS))
        return self._read("futures_exchange_info", "/fapi/v1/exchangeInfo", parse)

    @staticmethod
    def _filters(row):
        if row["quoteAsset"] != "USDT" or row["marginAsset"] != "USDT" or row["contractType"] != "PERPETUAL":
            _bad()
        filters = _rows(row["filters"])
        _unique(filters, lambda f: _text(f["filterType"]))
        fs = {f["filterType"]: f for f in filters}
        price, lot, market = fs["PRICE_FILTER"], fs["LOT_SIZE"], fs["MARKET_LOT_SIZE"]
        tick, lower, upper = (_decimal(price[k]) for k in ("tickSize", "minPrice", "maxPrice"))
        step, min_qty, max_qty = (_decimal(lot[k]) for k in ("stepSize", "minQty", "maxQty"))
        mstep, mmin, mmax = (_decimal(market[k]) for k in ("stepSize", "minQty", "maxQty"))
        # PRICE_FILTER explicitly permits zero to disable a rule. Quantity
        # increments must be usable; never substitute precision or another lot.
        if (upper != 0 and lower > upper) or max_qty < min_qty or mmax < mmin or (
                step <= 0 or mstep <= 0 or max_qty <= 0 or mmax <= 0):
            _bad()
        notional = _decimal(fs["MIN_NOTIONAL"]["notional"]) if "MIN_NOTIONAL" in fs else None
        # Unknown alternate notional semantics cannot be silently ignored.
        if "NOTIONAL" in fs:
            _bad()
        up = down = None
        if "PERCENT_PRICE" in fs:
            up = _decimal(fs["PERCENT_PRICE"]["multiplierUp"])
            down = _decimal(fs["PERCENT_PRICE"]["multiplierDown"])
            if up <= 0 or down <= 0 or down > up:
                _bad()
        return SymbolFilters(_text(row["symbol"]), _enum(row["status"], SYMBOL_STATUSES), tick, lower, upper,
                             step, min_qty, max_qty, mstep, mmin, mmax, notional,
                             _integer(row["quantityPrecision"]), _integer(row["pricePrecision"]), up, down)

    def get_account_info(self) -> AccountSnapshot:
        return self._read("futures_account", "/fapi/v2/account", lambda r, m: AccountSnapshot(
            m, _bool(r["canTrade"]), _bool(r["multiAssetsMargin"]),
            _decimal(r["totalWalletBalance"], signed=True), _decimal(r["totalMarginBalance"], signed=True),
            _decimal(r["availableBalance"], signed=True), _decimal(r["totalUnrealizedProfit"], signed=True)))

    def get_balances(self) -> BalancesSnapshot:
        def parse(raw, meta):
            items = [BalanceSnapshot(_text(r["asset"]), _decimal(r["balance"], signed=True),
                     _decimal(r["availableBalance"], signed=True), _decimal(r["crossWalletBalance"], signed=True),
                     _decimal(r["crossUnPnl"], signed=True), _integer(r["updateTime"])) for r in _rows(raw)]
            items = _unique(items, lambda b: b.asset)
            if not any(b.asset == "USDT" for b in items):
                _bad()
            return BalancesSnapshot(meta, items)
        return self._read("futures_account_balance", "/fapi/v3/balance", parse)

    def get_position_mode(self) -> str:
        def parse(raw, meta):
            if _bool(raw["dualSidePosition"]):
                raise AccountModeError("Hedge mode is unsupported; no mode change attempted")
            return "ONE_WAY"
        return self._read("futures_get_position_mode", "/fapi/v1/positionSide/dual", parse)

    def get_positions(self) -> PositionsSnapshot:
        def parse(raw, meta):
            positions = []
            for r in _rows(raw):
                positions.append(PositionSnapshot(_text(r["symbol"]), _decimal(r["positionAmt"], signed=True),
                    _enum(r["positionSide"], POSITION_SIDES), _decimal(r["entryPrice"]), _decimal(r["markPrice"]),
                    _decimal(r["unRealizedProfit"], signed=True), _decimal(r["liquidationPrice"]),
                    _text(r["marginAsset"]), _integer(r["updateTime"])))
            _unique(positions, lambda p: (p.symbol, p.position_side))
            issues = []
            if any(p.position_side != "BOTH" for p in positions):
                issues.append("HEDGE_STYLE_ROWS")
            if len({p.symbol for p in positions if p.quantity != 0}) > 1:
                issues.append("MULTIPLE_EXPOSURES")
            if any(p.symbol not in config.FIXED_SYMBOLS and p.quantity != 0 for p in positions):
                issues.append("OUTSIDE_WATCHLIST_EXPOSURE")
            if any(p.margin_asset != "USDT" and p.quantity != 0 for p in positions):
                issues.append("UNEXPECTED_MARGIN_ASSET")
            # This is specifically the unfiltered V3 endpoint, not a filtered,
            # paginated, or failed response. Only absent exposure is inferred.
            present = {p.symbol for p in positions}
            for symbol in config.FIXED_SYMBOLS:
                if symbol not in present:
                    positions.append(PositionSnapshot(symbol, Decimal("0"), None, None, None,
                                                      None, None, None, None, True))
            return PositionsSnapshot(meta, tuple(positions), tuple(issues))
        return self._read("futures_position_information", "/fapi/v3/positionRisk", parse)

    @staticmethod
    def _ordinary(r):
        quantity, executed = _decimal(r["origQty"]), _decimal(r["executedQty"])
        if executed > quantity:
            _bad()
        return OrdinaryOrderSnapshot(_text(r["symbol"]), _id(r["orderId"]), _text(r["clientOrderId"]),
            _enum(r["side"], SIDES), _enum(r["type"], ORDER_TYPES), _enum(r["status"], ORDER_STATUSES),
            quantity, executed, _decimal(r["price"]), _optional_decimal(r, "stopPrice"),
            _bool(r["reduceOnly"]), _bool(r["closePosition"]), _enum(r["positionSide"], POSITION_SIDES),
            None if "workingType" not in r else _enum(r["workingType"], WORKING_TYPES))

    @staticmethod
    def _algo(r):
        if r["algoType"] != "CONDITIONAL":
            _bad()
        close = _bool(r["closePosition"])
        quantity = _optional_decimal(r, "quantity")
        if quantity is None and not close:
            _bad()
        actual = r.get("actualOrderId")
        return AlgoOrderSnapshot(_text(r["symbol"]), _id(r["algoId"]), _text(r["clientAlgoId"]),
            _enum(r["side"], SIDES), _enum(r["orderType"], ORDER_TYPES - {"MARKET", "LIMIT"}),
            _enum(r["algoStatus"], ALGO_STATUSES), _decimal(r["triggerPrice"]), quantity,
            _bool(r["reduceOnly"]), close, _enum(r["positionSide"], POSITION_SIDES),
            _enum(r["workingType"], WORKING_TYPES), None if actual in (None, "") else _id(actual),
            _optional_decimal(r, "actualQty"))

    def get_open_ordinary_orders(self) -> OrdinaryOrdersSnapshot:
        def parse(raw, meta):
            orders = _unique([self._ordinary(o) for o in _rows(raw)], lambda o: (o.symbol, o.order_id))
            _unique(orders, lambda o: (o.symbol, o.client_order_id))
            return OrdinaryOrdersSnapshot(meta, orders)
        return self._read("futures_get_open_orders", "/fapi/v1/openOrders", parse)

    def get_open_algo_orders(self) -> AlgoOrdersSnapshot:
        def parse(raw, meta):
            orders = _unique([self._algo(o) for o in _rows(raw)], lambda o: (o.symbol, o.algo_id))
            _unique(orders, lambda o: (o.symbol, o.client_algo_id))
            return AlgoOrdersSnapshot(meta, orders)
        return self._read("futures_get_open_algo_orders", "/fapi/v1/openAlgoOrders", parse)

    def get_ordinary_order(self, symbol, *, order_id=None, client_order_id=None) -> OrdinaryOrderSnapshot:
        symbol = self._symbol(symbol)
        if (order_id is None) == (client_order_id is None):
            raise ConfigurationError("Supply exactly one ordinary order identity")
        params = {"symbol": symbol}
        params.update({"orderId": _id(order_id)} if order_id is not None else {"origClientOrderId": _text(client_order_id)})
        def parse(raw, meta):
            result = self._ordinary(raw)
            if result.symbol != symbol or (order_id is not None and result.order_id != params["orderId"]) or (
                    client_order_id is not None and result.client_order_id != client_order_id):
                _bad()
            return result
        return self._read("futures_get_order", "/fapi/v1/order", parse, **params)

    def get_algo_order(self, symbol, *, algo_id=None, client_algo_id=None) -> AlgoOrderSnapshot:
        symbol = self._symbol(symbol)
        if (algo_id is None) == (client_algo_id is None):
            raise ConfigurationError("Supply exactly one algo order identity")
        params = {"algoId": _id(algo_id)} if algo_id is not None else {"clientAlgoId": _text(client_algo_id)}
        def parse(raw, meta):
            result = self._algo(raw)
            if result.symbol != symbol or (algo_id is not None and result.algo_id != params["algoId"]) or (
                    client_algo_id is not None and result.client_algo_id != client_algo_id):
                _bad()
            return result
        return self._read("futures_get_algo_order", "/fapi/v1/algoOrder", parse, **params)

    def get_recent_fills(self, symbol, *, limit=500, from_id=None) -> FillsSnapshot:
        symbol = self._symbol(symbol)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ConfigurationError("Fill limit must be between 1 and 1000")
        params = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = _id(from_id)
        def parse(raw, meta):
            items = [FillSnapshot(_text(r["symbol"]), _id(r["id"]), _id(r["orderId"]),
                     _enum(r["side"], SIDES), _enum(r["positionSide"], POSITION_SIDES),
                     _decimal(r["price"]), _decimal(r["qty"]), _decimal(r["realizedPnl"], signed=True),
                     _decimal(r["commission"], signed=True), _text(r["commissionAsset"]), _integer(r["time"]))
                     for r in _rows(raw)]
            if len(items) > limit or any(f.symbol != symbol for f in items):
                _bad()
            return FillsSnapshot(meta, _unique(items, lambda f: (f.symbol, f.trade_id)), len(items) == limit)
        return self._read("futures_account_trades", "/fapi/v1/userTrades", parse, **params)
