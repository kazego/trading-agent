"""Explicit operator-only Demo contract checks. Importing this module does no I/O.

Run: python demo_contract.py --live-read-only
Never use bot.py for contract validation.
"""

import os
import sys
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from importlib.metadata import version
from types import UnionType
from typing import get_args, get_origin, get_type_hints

import models
from errors import (AccountModeError, AuthenticationError, ConfigurationError,
                    RateLimitError, ReadTimeoutError, TransientReadError,
                    UnknownExchangeStateError)

HOST = 'https://demo-fapi.binance.com'
SDK_VERSION = '1.0.37'
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'ADAUSDT')
KEY_VARIABLE = 'BINANCE_DEMO_API_KEY'
SECRET_VARIABLE = 'BINANCE_DEMO_API_SECRET'


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class ContractReport:
    checks: tuple[CheckResult, ...]
    overall: str


def _matches(value, annotation):
    origin = get_origin(annotation)
    if origin is UnionType:
        return any(_matches(value, option) for option in get_args(annotation))
    if origin is tuple:
        args = get_args(annotation)
        return type(value) is tuple and len(args) == 2 and args[1] is Ellipsis and all(
            _matches(item, args[0]) for item in value)
    if type(value) is not annotation:
        return False
    if annotation is Decimal:
        return value.is_finite()
    if is_dataclass(value):
        hints = get_type_hints(annotation)
        return all(_matches(getattr(value, f.name), hints[f.name]) for f in fields(value))
    return True


def _expect(value, annotation):
    if not _matches(value, annotation):
        raise UnknownExchangeStateError('Normalized model contract mismatch')
    return value


def _failure(error):
    # Fixed reason labels only: never emit exception text or arbitrary class names.
    for kind, label, overall in (
        (AuthenticationError, 'AUTHENTICATION', 'FAIL'),
        (ConfigurationError, 'ENVIRONMENT_OR_CONFIGURATION', 'FAIL'),
        (AccountModeError, 'UNSUPPORTED_ACCOUNT_MODE', 'FAIL'),
        (ReadTimeoutError, 'TRANSIENT_TIMEOUT', 'PARTIAL'),
        (RateLimitError, 'RATE_LIMIT', 'PARTIAL'),
        (TransientReadError, 'TRANSIENT_READ', 'PARTIAL'),
    ):
        if isinstance(error, kind):
            return label, overall
    return 'MALFORMED_OR_UNKNOWN_STATE', 'FAIL'


def _run_checks(adapter):
    """Called only after CLI gates; tests supply a fake adapter, never the SDK."""
    values = {}
    checks = []

    def save(name, call, expected):
        value = _expect(call(), expected)
        values[name] = value
        return value

    def filters(symbol):
        items = values['exchange'].symbols
        if tuple(f.symbol for f in items) != SYMBOLS:
            raise UnknownExchangeStateError('Watchlist metadata mismatch')
        return _expect(next(f for f in items if f.symbol == symbol), models.SymbolFilters)

    def mode():
        result = _expect(adapter.get_position_mode(), str)
        if result != 'ONE_WAY':
            raise AccountModeError('One-Way required')
        return result

    def positions():
        result = _expect(adapter.get_positions(), models.PositionsSnapshot)
        if result.issues or not set(SYMBOLS).issubset({p.symbol for p in result.positions}):
            raise UnknownExchangeStateError('Inconsistent position snapshot')
        if any(p.position_side not in (None, 'BOTH') for p in result.positions):
            raise AccountModeError('Hedge-style position row')
        return result

    def orders(family):
        ordinary = family == 'ordinary'
        value = save(family, adapter.get_open_ordinary_orders if ordinary else adapter.get_open_algo_orders,
                     models.OrdinaryOrdersSnapshot if ordinary else models.AlgoOrdersSnapshot)
        if any(o.symbol not in SYMBOLS for o in value.orders):
            raise UnknownExchangeStateError('Order outside watchlist')
        if any(o.position_side != 'BOTH' for o in value.orders):
            raise AccountModeError('Hedge-style order row')
        return value

    def lookup(family, by_client):
        existing = values[family].orders
        if not existing:
            return None
        # One representative existing order per family, using BOTH ID forms.
        row = existing[0]
        if family == 'ordinary':
            kwargs = {'client_order_id': row.client_order_id} if by_client else {'order_id': row.order_id}
            result = _expect(adapter.get_ordinary_order(row.symbol, **kwargs), models.OrdinaryOrderSnapshot)
            if (result.symbol, result.order_id, result.client_order_id) != (
                    row.symbol, row.order_id, row.client_order_id):
                raise UnknownExchangeStateError('Lookup identity mismatch')
        else:
            kwargs = {'client_algo_id': row.client_algo_id} if by_client else {'algo_id': row.algo_id}
            result = _expect(adapter.get_algo_order(row.symbol, **kwargs), models.AlgoOrderSnapshot)
            if (result.symbol, result.algo_id, result.client_algo_id) != (
                    row.symbol, row.algo_id, row.client_algo_id):
                raise UnknownExchangeStateError('Lookup identity mismatch')
        if result.position_side != 'BOTH':
            raise AccountModeError('Hedge-style lookup row')
        return result

    plan = [
        ('server time', lambda: _expect(adapter.get_server_time(), int)),
        ('exchange info', lambda: save('exchange', adapter.get_exchange_info, models.ExchangeInfoSnapshot)),
        *[(s + ' filters', lambda s=s: filters(s)) for s in SYMBOLS],
        ('account info', lambda: _expect(adapter.get_account_info(), models.AccountSnapshot)),
        ('balances', lambda: _expect(adapter.get_balances(), models.BalancesSnapshot)),
        ('position mode', mode),
        ('positions', positions),
        ('ordinary orders', lambda: orders('ordinary')),
        ('algo orders', lambda: orders('algo')),
        *[(s + ' fills', lambda s=s: _expect(adapter.get_recent_fills(s, limit=100), models.FillsSnapshot)) for s in SYMBOLS],
        ('ordinary lookup exchange ID', lambda: lookup('ordinary', False)),
        ('ordinary lookup client ID', lambda: lookup('ordinary', True)),
        ('algo lookup exchange ID', lambda: lookup('algo', False)),
        ('algo lookup client ID', lambda: lookup('algo', True)),
    ]
    overall = 'PASS'
    for index, (name, call) in enumerate(plan):
        try:
            value = call()
            if value is None:
                checks.append(CheckResult(name, 'SKIPPED', 'NO_EXISTING_ORDER'))
                overall = 'PARTIAL'
                continue
            detail = 'TYPED_NORMALIZATION_ACCEPTED'
            if name == 'server time':
                if value < 0:
                    raise UnknownExchangeStateError('Invalid server time')
                detail = 'serverTime=' + str(value)
            elif name == 'position mode':
                detail = 'ONE_WAY'
            elif isinstance(value, models.PositionsSnapshot):
                detail += '; inferred_flat_rows=' + str(sum(p.inferred_flat for p in value.positions))
            elif isinstance(value, models.OrdinaryOrdersSnapshot):
                detail += '; returned_rows=' + str(len(value.orders))
                detail += '; absent_stop_price=' + str(sum(o.stop_price is None for o in value.orders))
                detail += '; absent_working_type=' + str(sum(o.working_type is None for o in value.orders))
            elif isinstance(value, models.AlgoOrdersSnapshot):
                detail += '; returned_rows=' + str(len(value.orders))
                detail += '; absent_quantity=' + str(sum(o.quantity is None for o in value.orders))
                detail += '; actual_link_present=' + str(sum(o.actual_order_id is not None for o in value.orders))
            elif isinstance(value, models.FillsSnapshot):
                detail += '; returned_rows=' + str(len(value.fills))
                if value.limit_reached:
                    detail += '; BOUNDED_PAGE_ONLY'
            checks.append(CheckResult(name, 'PASS', detail))
        except Exception as error:
            reason, overall = _failure(error)
            checks.append(CheckResult(name, 'FAIL', reason))
            checks.extend(CheckResult(later, 'SKIPPED', 'STOPPED_AFTER_FAILURE') for later, _ in plan[index + 1:])
            break  # Stop even on transient failures; no retries or subsequent reads.
    return ContractReport(tuple(checks), overall)


def _under_pytest(environ):
    return 'pytest' in sys.modules or 'PYTEST_CURRENT_TEST' in os.environ or 'PYTEST_CURRENT_TEST' in environ


def main(argv=None, *, environ=None, adapter_factory=None, version_reader=version, output=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    output = sys.stdout if output is None else output
    key, secret = environ.get(KEY_VARIABLE, ''), environ.get(SECRET_VARIABLE, '')

    def emit(text):
        # Defense in depth, including keys which happen to match public metadata.
        for credential in sorted((v for v in (key, secret) if isinstance(v, str) and v), key=len, reverse=True):
            text = text.replace(credential, '[REDACTED]')
        print(text, file=output)

    if _under_pytest(environ):
        emit('FAIL invocation: PYTEST_EXECUTION_REFUSED\nOVERALL FAIL')
        return 1
    if argv != ['--live-read-only']:
        emit('SKIPPED invocation: explicitly run python demo_contract.py --live-read-only\nOVERALL PARTIAL')
        return 2
    if not all(isinstance(v, str) and v.strip() for v in (key, secret)):
        emit('FAIL credentials: BINANCE_DEMO_API_KEY and BINANCE_DEMO_API_SECRET are required\nOVERALL FAIL')
        return 1
    try:
        if version_reader('python-binance') != SDK_VERSION:
            raise ConfigurationError('Pinned SDK required')
        if adapter_factory is None:
            from binance_client import BinanceFuturesClient
            adapter_factory = BinanceFuturesClient
        adapter = adapter_factory(api_key=key, api_secret=secret)
        adapter._validate_environment()  # Existing adapter guard; no direct SDK operation.
    except Exception as error:
        reason = 'SDK_UNAVAILABLE' if isinstance(error, (ImportError, ModuleNotFoundError)) else _failure(error)[0]
        emit('FAIL initialization: ' + reason + '\nOVERALL FAIL')
        return 1
    emit('PASS environment: resolved Futures host=' + HOST + '; SDK=' + SDK_VERSION)
    report = _run_checks(adapter)
    for check in report.checks:
        emit(check.status + ' ' + check.name + ': ' + check.detail)
    emit('SKIPPED raw shape comparison: adapter normalization hides extra raw fields; no raw payloads inspected or retained')
    emit('OVERALL ' + report.overall)
    return {'PASS': 0, 'FAIL': 1, 'PARTIAL': 2}[report.overall]


if __name__ == '__main__':
    raise SystemExit(main())
