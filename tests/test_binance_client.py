"""Synthetic SDK only: no installed SDK, credentials, network, or exchange calls."""

import ast
import copy
import importlib.util
import sys
import types
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import config
from errors import (AccountModeError, AuthenticationError, ConfigurationError,
                    RateLimitError, ReadTimeoutError, TransientReadError,
                    UnknownExchangeStateError, classify_error)


class FakeAPIError(Exception):
    def __init__(self, code, status_code):
        super().__init__('synthetic-secret: do not disclose')
        self.code = code
        self.status_code = status_code


class FakeTimeout(Exception):
    pass


class FakeConnectionError(Exception):
    pass


def symbol_row(symbol, tick='0.1', step='0.001'):
    return dict(symbol=symbol, status='TRADING', quoteAsset='USDT', marginAsset='USDT',
                contractType='PERPETUAL', quantityPrecision=3, pricePrecision=2,
                filters=[dict(filterType='PRICE_FILTER', minPrice='0.1', maxPrice='1000000', tickSize=tick),
                         dict(filterType='LOT_SIZE', minQty=step, maxQty='100', stepSize=step),
                         dict(filterType='MARKET_LOT_SIZE', minQty=step, maxQty='20', stepSize=step),
                         dict(filterType='MIN_NOTIONAL', notional='5'),
                         dict(filterType='PERCENT_PRICE', multiplierUp='1.05', multiplierDown='0.95')])


def position(symbol='BTCUSDT', quantity='0.01', side='BOTH'):
    return dict(symbol=symbol, positionAmt=quantity, positionSide=side, entryPrice='60000',
                markPrice='60001', unRealizedProfit='0.01', liquidationPrice='0',
                marginAsset='USDT', updateTime=100)


def ordinary(**overrides):
    row = dict(symbol='BTCUSDT', orderId=123, clientOrderId='ordinary-123', side='SELL', type='LIMIT',
               status='NEW', origQty='0.01', executedQty='0', price='61000', stopPrice='0',
               reduceOnly=True, closePosition=False, positionSide='BOTH', workingType='CONTRACT_PRICE')
    row.update(overrides)
    return row


def algo(**overrides):
    row = dict(symbol='BTCUSDT', algoId=123, clientAlgoId='algo-123', algoType='CONDITIONAL',
               side='SELL', orderType='STOP_MARKET', algoStatus='NEW', triggerPrice='59000',
               quantity='0', reduceOnly=False, closePosition=True, positionSide='BOTH',
               workingType='MARK_PRICE', actualOrderId='')
    row.update(overrides)
    return row


class FakeSDK:
    def __init__(self):
        self.demo = True
        self.testnet = False
        self.base = 'https://demo-fapi.binance.com/fapi'
        self.calls = []
        self.responses = {}

    def _create_futures_api_uri(self, path, version=1):
        return self.base + '/v' + str(version) + '/' + path

    def __getattr__(self, name):
        # Only reads explicitly given a fake response can be called.
        if name not in self.responses:
            raise AssertionError('Unexpected SDK call: ' + name)
        def read(**params):
            self.calls.append((name, params))
            response = self.responses[name]
            if isinstance(response, Exception):
                raise response
            return copy.deepcopy(response)
        return read


class DemoAdapterTests(unittest.TestCase):
    def setUp(self):
        client_module = types.ModuleType('binance.client')
        client_module.Client = Mock(side_effect=AssertionError('real factory forbidden'))
        sdk_errors = types.ModuleType('binance.exceptions')
        sdk_errors.BinanceAPIException = FakeAPIError
        request_errors = types.ModuleType('requests.exceptions')
        request_errors.Timeout = FakeTimeout
        request_errors.ConnectionError = FakeConnectionError
        with patch.dict(sys.modules, {'binance': types.ModuleType('binance'), 'binance.client': client_module,
                                      'binance.exceptions': sdk_errors, 'requests': types.ModuleType('requests'),
                                      'requests.exceptions': request_errors}):
            spec = importlib.util.spec_from_file_location('offline_demo_adapter', config.PROJECT_ROOT / 'binance_client.py')
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.sdk = FakeSDK()
        self.factory = Mock(return_value=self.sdk)
        self.client = self.construct()

    def construct(self):
        return self.module.BinanceFuturesClient(api_key='synthetic-key', api_secret='synthetic-secret',
                                               sdk_factory=self.factory)

    def response(self, method, value):
        self.sdk.responses[method] = value

    def test_demo_construction_is_network_free_and_read_only(self):
        self.factory.assert_called_once_with('synthetic-key', 'synthetic-secret', demo=True, testnet=False,
            ping=False, verbose=False, requests_params={'timeout': config.API_TIMEOUT_SECONDS, 'allow_redirects': False})
        self.assertEqual(self.sdk.calls, [])

    def test_production_legacy_and_url_variants_are_rejected(self):
        for url in ('https://fapi.binance.com/fapi', 'https://testnet.binancefuture.com/fapi',
                    'http://demo-fapi.binance.com/fapi', 'https://demo-fapi.binance.com.evil/fapi',
                    'https://demo-fapi.binance.com/fapi/', 'https://user@demo-fapi.binance.com/fapi'):
            with self.subTest(url=url), self.assertRaises(ConfigurationError):
                self.sdk.base = url
                self.construct()
        self.assertEqual(self.sdk.calls, [])

    def test_wrong_flags_or_config_are_rejected(self):
        for demo, testnet in ((False, False), (True, True), (1, False)):
            self.sdk.demo, self.sdk.testnet = demo, testnet
            with self.assertRaises(ConfigurationError):
                self.construct()
        with patch.object(config, 'USE_TESTNET', False), self.assertRaises(ConfigurationError):
            self.construct()

    def test_invalid_timeout_is_rejected_before_construction(self):
        self.factory.reset_mock()
        for value in (None, 0, -1, True):
            with patch.object(config, 'API_TIMEOUT_SECONDS', value), self.assertRaises(ConfigurationError):
                self.construct()
        self.factory.assert_not_called()

    def test_endpoint_checked_again_before_reads(self):
        self.response('futures_time', {'serverTime': 100})
        self.sdk.base = 'https://fapi.binance.com/fapi'
        with self.assertRaises(ConfigurationError):
            self.client.get_server_time()
        self.assertEqual(self.sdk.calls, [])

    def test_missing_credentials_fail_before_factory(self):
        self.factory.reset_mock()
        with self.assertRaises(ConfigurationError):
            self.module.BinanceFuturesClient(api_key='', api_secret='', sdk_factory=self.factory)
        self.factory.assert_not_called()

    def test_server_time_and_malformed_response(self):
        self.response('futures_time', {'serverTime': 100})
        self.assertEqual(self.client.get_server_time(), 100)
        for value in ({}, [], {'serverTime': True}, {'serverTime': -1}, {'code': -1000}):
            self.response('futures_time', value)
            with self.subTest(value=value), self.assertRaises(UnknownExchangeStateError):
                self.client.get_server_time()

    def exchange(self):
        data = {'symbols': [symbol_row('BTCUSDT'), symbol_row('ETHUSDT', '0.01', '0.01'),
                            symbol_row('ADAUSDT', '0.0001', '1')]}
        self.response('futures_exchange_info', data)
        return data

    def test_all_watchlist_filters_use_independent_decimal_rules(self):
        self.exchange()
        snapshot = self.client.get_exchange_info()
        self.assertEqual([s.symbol for s in snapshot.symbols], list(config.FIXED_SYMBOLS))
        self.assertEqual([s.tick_size for s in snapshot.symbols], [Decimal('0.1'), Decimal('0.01'), Decimal('0.0001')])
        self.assertEqual([s.step_size for s in snapshot.symbols], [Decimal('0.001'), Decimal('0.01'), Decimal('1')])
        self.assertEqual(snapshot.symbols[0].max_qty, Decimal('100'))
        self.assertEqual(snapshot.symbols[0].market_max_qty, Decimal('20'))
        self.assertEqual(snapshot.symbols[0].min_notional, Decimal('5'))
        self.assertEqual(snapshot.symbols[0].percent_price_down, Decimal('0.95'))
        self.assertEqual(snapshot.metadata.source, '/fapi/v1/exchangeInfo')
        self.assertGreaterEqual(snapshot.metadata.received_at_ms, snapshot.metadata.requested_at_ms)
        with self.assertRaises(FrozenInstanceError):
            snapshot.symbols[0].tick_size = Decimal('2')

    def test_optional_notional_and_percent_rules_are_not_invented(self):
        data = self.exchange()
        data['symbols'][0]['filters'] = data['symbols'][0]['filters'][:3]
        result = self.client.get_exchange_info().symbols[0]
        self.assertIsNone(result.min_notional)
        self.assertIsNone(result.percent_price_up)

    def test_missing_symbol_filter_and_duplicate_filter_fail_closed(self):
        for edit in (lambda d: d['symbols'].pop(), lambda d: d['symbols'][0]['filters'].pop(0),
                     lambda d: d['symbols'][0]['filters'].append(d['symbols'][0]['filters'][0]),
                     lambda d: d['symbols'].append(d['symbols'][0])):
            data = self.exchange()
            edit(data)
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_exchange_info()

    def test_malformed_filter_financial_values_and_bounds(self):
        for value in (0.1, 1, True, None, 'NaN', 'Infinity', '-0.1', 'abc'):
            data = self.exchange()
            data['symbols'][0]['filters'][0]['tickSize'] = value
            with self.subTest(value=value), self.assertRaises(UnknownExchangeStateError):
                self.client.get_exchange_info()
        data = self.exchange()
        data['symbols'][0]['filters'][1]['minQty'] = '200'
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_exchange_info()

    def test_wrong_symbol_contract_and_notional_schema_are_rejected(self):
        for field, value in (('quoteAsset', 'BTC'), ('marginAsset', 'USDC'), ('contractType', 'CURRENT_QUARTER'),
                             ('pricePrecision', 1.2), ('status', 'UNKNOWN_STATUS')):
            data = self.exchange()
            data['symbols'][0][field] = value
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_exchange_info()
        data = self.exchange()
        data['symbols'][0]['filters'].append({'filterType': 'NOTIONAL', 'minNotional': '5'})
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_exchange_info()

    def test_disabled_price_rules_and_invalid_quantity_steps(self):
        data = self.exchange()
        data['symbols'][0]['filters'][0].update(minPrice='0', maxPrice='0', tickSize='0')
        self.assertEqual(self.client.get_exchange_info().symbols[0].tick_size, Decimal('0'))
        for index in (1, 2):
            data = self.exchange()
            data['symbols'][0]['filters'][index]['stepSize'] = '0'
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_exchange_info()

    def test_account_financial_values_and_mode_preserved(self):
        self.response('futures_account', dict(canTrade=True, multiAssetsMargin=False, totalWalletBalance='100.01',
            totalMarginBalance='99.01', availableBalance='90', totalUnrealizedProfit='-1'))
        result = self.client.get_account_info()
        self.assertEqual(result.unrealized_pnl, Decimal('-1'))
        self.assertEqual(result.margin_balance, Decimal('99.01'))
        self.assertFalse(result.multi_assets_margin)
        self.assertEqual(result.metadata.source, '/fapi/v2/account')

    def test_account_missing_fields_and_float_fail_closed(self):
        self.response('futures_account', {})
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_account_info()
        self.response('futures_account', dict(canTrade=True, multiAssetsMargin=False, totalWalletBalance=100.01,
            totalMarginBalance='99', availableBalance='90', totalUnrealizedProfit='-1'))
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_account_info()

    def test_balances_require_usdt_and_unique_assets(self):
        balance = dict(asset='USDT', balance='100', availableBalance='90', crossWalletBalance='95',
                       crossUnPnl='-2', updateTime=123)
        self.response('futures_account_balance', [balance])
        self.assertEqual(self.client.get_balances().balances[0].cross_unrealized_pnl, Decimal('-2'))
        for raw in ([], [balance, balance], {}, [{**balance, 'asset': 'BTC'}]):
            self.response('futures_account_balance', raw)
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_balances()

    def test_one_way_and_unsupported_mode(self):
        self.response('futures_get_position_mode', {'dualSidePosition': False})
        self.assertEqual(self.client.get_position_mode(), 'ONE_WAY')
        self.response('futures_get_position_mode', {'dualSidePosition': 'true'})
        with self.assertRaises(AccountModeError):
            self.client.get_position_mode()
        self.response('futures_get_position_mode', {'dualSidePosition': 0})
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_position_mode()

    def test_empty_successful_account_wide_v3_means_absent_exposure(self):
        self.response('futures_position_information', [])
        snapshot = self.client.get_positions()
        self.assertTrue(snapshot.flat)
        self.assertEqual([p.symbol for p in snapshot.positions], list(config.FIXED_SYMBOLS))
        self.assertTrue(all(p.inferred_flat and p.quantity == Decimal('0') and p.entry_price is None
                            and p.position_side is None for p in snapshot.positions))
        self.assertEqual(self.sdk.calls, [('futures_position_information', {})])

    def test_explicit_flat_row_is_distinct_from_inferred_flat(self):
        self.response('futures_position_information', [position(quantity='0')])
        snapshot = self.client.get_positions()
        self.assertTrue(snapshot.flat)
        self.assertFalse(snapshot.positions[0].inferred_flat)

    def test_long_and_short_are_preserved(self):
        for qty, direction in (('0.1', 'LONG'), ('-0.1', 'SHORT')):
            self.response('futures_position_information', [position('ETHUSDT', qty)])
            snapshot = self.client.get_positions()
            self.assertEqual(snapshot.positions[0].direction, direction)
            self.assertEqual(snapshot.exposure_symbols, ('ETHUSDT',))
            self.assertFalse(snapshot.flat)

    def test_multiple_exposure_is_preserved_not_resolved(self):
        self.response('futures_position_information', [position(), position('ADAUSDT', '-100')])
        snapshot = self.client.get_positions()
        self.assertEqual(snapshot.exposure_symbols, ('ADAUSDT', 'BTCUSDT'))
        self.assertIn('MULTIPLE_EXPOSURES', snapshot.issues)
        self.assertEqual(len(self.sdk.calls), 1)

    def test_hedge_style_rows_are_visible_and_flagged(self):
        self.response('futures_position_information', [position(side='LONG'), position(quantity='-0.02', side='SHORT')])
        snapshot = self.client.get_positions()
        self.assertIn('HEDGE_STYLE_ROWS', snapshot.issues)
        self.assertEqual([p.position_side for p in snapshot.positions[:2]], ['LONG', 'SHORT'])
        self.assertFalse(snapshot.flat)

    def test_outside_watchlist_exposure_is_not_hidden(self):
        self.response('futures_position_information', [position('SOLUSDT', '1')])
        snapshot = self.client.get_positions()
        self.assertIn('OUTSIDE_WATCHLIST_EXPOSURE', snapshot.issues)
        self.assertEqual(snapshot.positions[0].symbol, 'SOLUSDT')

    def test_invalid_positions_never_become_flat(self):
        for raw in ({}, None, {'code': -1001}, [position(), position()], [{'symbol': 'BTCUSDT'}],
                    [position(quantity=0.1)], [position(side='unexpected')]):
            self.response('futures_position_information', raw)
            with self.subTest(raw=raw), self.assertRaises(UnknownExchangeStateError):
                self.client.get_positions()

    def test_ordinary_normalization(self):
        self.response('futures_get_open_orders', [ordinary()])
        result = self.client.get_open_ordinary_orders().orders[0]
        self.assertEqual(result.order_id, '123')
        self.assertEqual(result.client_order_id, 'ordinary-123')
        self.assertEqual(result.quantity, Decimal('0.01'))
        self.assertTrue(result.reduce_only)
        self.assertFalse(result.close_position)

    def test_algo_normalization_and_linked_actual_order(self):
        self.response('futures_get_open_algo_orders', [algo(algoStatus='TRIGGERED', actualOrderId='456', actualQty='0.01')])
        result = self.client.get_open_algo_orders().orders[0]
        self.assertEqual(result.algo_id, '123')
        self.assertEqual(result.actual_order_id, '456')
        self.assertEqual(result.trigger_price, Decimal('59000'))
        self.assertEqual(result.actual_quantity, Decimal('0.01'))
        self.assertTrue(result.close_position)
        self.assertFalse(hasattr(result, 'order_id'))

    def test_close_position_quantity_can_be_absent(self):
        row = algo()
        del row['quantity']
        self.response('futures_get_open_algo_orders', [row])
        self.assertIsNone(self.client.get_open_algo_orders().orders[0].quantity)
        row['closePosition'] = False
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_open_algo_orders()

    def test_empty_order_lists_and_namespaces(self):
        self.response('futures_get_open_orders', [])
        self.response('futures_get_open_algo_orders', [])
        self.assertEqual(self.client.get_open_ordinary_orders().orders, ())
        self.assertEqual(self.client.get_open_algo_orders().orders, ())
        self.response('futures_get_open_orders', [ordinary()])
        self.response('futures_get_open_algo_orders', [algo()])
        self.assertEqual(self.client.get_open_ordinary_orders().orders[0].order_id,
                         self.client.get_open_algo_orders().orders[0].algo_id)

    def test_duplicate_ids_and_malformed_order_envelopes(self):
        for method, call, row in (('futures_get_open_orders', self.client.get_open_ordinary_orders, ordinary()),
                                  ('futures_get_open_algo_orders', self.client.get_open_algo_orders, algo())):
            for response in ([row, row], {}, {'orders': []}, None):
                self.response(method, response)
                with self.assertRaises(UnknownExchangeStateError):
                    call()

    def test_duplicate_open_client_ids_are_rejected_within_each_symbol_family(self):
        self.response('futures_get_open_orders', [ordinary(), ordinary(orderId=456)])
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_open_ordinary_orders()
        self.response('futures_get_open_algo_orders', [algo(), algo(algoId=456)])
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_open_algo_orders()

    def test_malformed_ids_status_and_booleans(self):
        for field, value in (('orderId', None), ('orderId', True), ('orderId', 'abc'), ('orderId', 0),
                             ('status', 'UNRECOGNIZED'), ('clientOrderId', ''), ('reduceOnly', 1),
                             ('executedQty', '1')):
            self.response('futures_get_open_orders', [ordinary(**{field: value})])
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_open_ordinary_orders()
        for field, value in (('algoId', 'bad'), ('algoStatus', 'UNKNOWN_NEW_STATUS'), ('orderType', 'MARKET'),
                             ('actualOrderId', '0'), ('triggerPrice', 1.0), ('algoType', 'TWAP')):
            self.response('futures_get_open_algo_orders', [algo(**{field: value})])
            with self.assertRaises(UnknownExchangeStateError):
                self.client.get_open_algo_orders()

    def test_ordinary_lookup_by_both_identity_forms(self):
        self.response('futures_get_order', ordinary())
        self.assertEqual(self.client.get_ordinary_order('BTCUSDT', order_id=123).order_id, '123')
        self.assertEqual(self.sdk.calls[-1][1], {'symbol': 'BTCUSDT', 'orderId': '123'})
        self.client.get_ordinary_order('BTCUSDT', client_order_id='ordinary-123')
        self.assertEqual(self.sdk.calls[-1][1], {'symbol': 'BTCUSDT', 'origClientOrderId': 'ordinary-123'})
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_ordinary_order('ETHUSDT', order_id=123)
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_ordinary_order('BTCUSDT', order_id=456)

    def test_algo_lookup_uses_separate_identity_fields(self):
        self.response('futures_get_algo_order', algo())
        self.client.get_algo_order('BTCUSDT', algo_id=123)
        self.assertEqual(self.sdk.calls[-1][1], {'algoId': '123'})
        self.client.get_algo_order('BTCUSDT', client_algo_id='algo-123')
        self.assertEqual(self.sdk.calls[-1][1], {'clientAlgoId': 'algo-123'})
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_algo_order('BTCUSDT', client_algo_id='different')

    def test_lookup_requires_exactly_one_identity_and_fixed_symbol(self):
        for call in (lambda: self.client.get_ordinary_order('BTCUSDT'),
                     lambda: self.client.get_ordinary_order('BTCUSDT', order_id=1, client_order_id='x'),
                     lambda: self.client.get_algo_order('BTCUSDT'),
                     lambda: self.client.get_algo_order('SOLUSDT', algo_id=1)):
            with self.assertRaises(ConfigurationError):
                call()
        self.assertEqual(self.sdk.calls, [])

    def test_fills_are_bounded_decimal_pages(self):
        fill = dict(symbol='BTCUSDT', id=1, orderId=123, side='SELL', positionSide='BOTH', price='60001',
                    qty='0.01', realizedPnl='-0.1', commission='0.001', commissionAsset='USDT', time=100)
        self.response('futures_account_trades', [fill])
        result = self.client.get_recent_fills('BTCUSDT', limit=1, from_id=1)
        self.assertEqual(result.fills[0].realized_pnl, Decimal('-0.1'))
        self.assertTrue(result.limit_reached)
        self.assertEqual(self.sdk.calls[-1][1], {'symbol': 'BTCUSDT', 'limit': 1, 'fromId': '1'})
        self.response('futures_account_trades', [fill, fill])
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_recent_fills('BTCUSDT')
        self.response('futures_account_trades', [fill])
        with self.assertRaises(UnknownExchangeStateError):
            self.client.get_recent_fills('ETHUSDT')

    def test_timeout_error_mapping_and_no_retry(self):
        for error in (FakeTimeout('synthetic-secret'), TimeoutError('synthetic-secret'), FakeAPIError(-1007, 400)):
            self.check_error(error, ReadTimeoutError, True)

    def test_authentication_error_mapping(self):
        for error in (FakeAPIError(-2014, 400), FakeAPIError(-2015, 400), FakeAPIError(-1022, 400),
                      FakeAPIError(0, 401), FakeAPIError(0, 403)):
            self.check_error(error, AuthenticationError, False)

    def test_rate_limit_error_mapping(self):
        for error in (FakeAPIError(-1003, 400), FakeAPIError(0, 418), FakeAPIError(0, 429)):
            self.check_error(error, RateLimitError, True)

    def test_transient_error_mapping(self):
        for error in (FakeConnectionError('synthetic-secret'), FakeAPIError(-1001, 400), FakeAPIError(0, 503)):
            self.check_error(error, TransientReadError, True)

    def test_unknown_errors_are_nonrecoverable(self):
        for error in (RuntimeError('synthetic-secret'), ValueError('synthetic-secret'),
                      FakeAPIError(-2013, 400), FakeAPIError(-1021, 400)):
            self.check_error(error, UnknownExchangeStateError, False)

    def check_error(self, error, kind, recoverable):
        before = len(self.sdk.calls)
        self.response('futures_time', error)
        with self.assertRaises(kind) as caught:
            self.client.get_server_time()
        self.assertNotIn('synthetic-secret', str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(classify_error(caught.exception).recoverable, recoverable)
        self.assertTrue(classify_error(caught.exception).blocks_entries)
        self.assertEqual(len(self.sdk.calls), before + 1)

    def test_no_exchange_mutation_methods_or_legacy_execution_interface(self):
        for name in ('open_market_position', 'set_stop_loss_take_profit', 'close_position_market',
                     'cancel_all_open_orders', 'has_protective_orders'):
            self.assertFalse(hasattr(self.client, name))
        tree = ast.parse((config.PROJECT_ROOT / 'binance_client.py').read_text(encoding='utf-8-sig'))
        forbidden = ('create_order', 'cancel', 'change_leverage', 'change_margin', 'change_position')
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith('futures_'):
                self.assertFalse(any(part in node.value for part in forbidden))
