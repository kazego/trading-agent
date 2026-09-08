"""Offline safety/classification tests. All successful runs use the Phase 4B fake SDK."""

import importlib.util
import io
import sys
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import demo_contract as contract
import test_binance_client as fixtures
from errors import (AccountModeError, AuthenticationError, ConfigurationError, RateLimitError,
                    ReadTimeoutError, TransientReadError, UnknownExchangeStateError)


class ContractUtilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DemoAdapterTests()
        self.fixture.setUp()
        self.adapter = self.fixture.client
        self.sdk = self.fixture.sdk
        self.factory = Mock(return_value=self.adapter)
        self.version = Mock(return_value='1.0.37')
        self.env = {'BINANCE_DEMO_API_KEY': 'synthetic-demo-key',
                    'BINANCE_DEMO_API_SECRET': 'synthetic-demo-secret'}
        self.fixture.exchange()
        self.sdk.responses.update({
            'futures_time': {'serverTime': 100},
            'futures_account': dict(canTrade=True, multiAssetsMargin=False, totalWalletBalance='100',
                totalMarginBalance='100', availableBalance='100', totalUnrealizedProfit='0'),
            'futures_account_balance': [dict(asset='USDT', balance='100', availableBalance='100',
                crossWalletBalance='100', crossUnPnl='0', updateTime=100)],
            'futures_get_position_mode': {'dualSidePosition': False},
            'futures_position_information': [],
            'futures_get_open_orders': [],
            'futures_get_open_algo_orders': [],
            'futures_account_trades': [],
        })

    def run_fake(self, *, argv=None, environ=None):
        output = io.StringIO()
        # This bypass is confined to a fake-adapter unit test, including pytest runs.
        with patch.object(contract, '_under_pytest', return_value=False):
            code = contract.main(['--live-read-only'] if argv is None else argv,
                environ=self.env if environ is None else environ, adapter_factory=self.factory,
                version_reader=self.version, output=output)
        return code, output.getvalue()

    def test_import_never_invokes_adapter_or_reads(self):
        with patch.dict(sys.modules, {'binance_client': None}):
            spec = importlib.util.spec_from_file_location('import_only_contract', contract.__file__)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {'import_only_contract': module}):
                spec.loader.exec_module(module)
        self.assertEqual(self.sdk.calls, [])

    def test_explicit_flag_required_and_unknown_arguments_not_echoed(self):
        for argv in ([], ['--help'], ['synthetic-demo-secret'], ['--live-read-only', '--force']):
            code, text = self.run_fake(argv=argv)
            self.assertEqual(code, 2)
            self.assertIn('OVERALL PARTIAL', text)
            self.assertNotIn(self.env['BINANCE_DEMO_API_SECRET'], text)
        self.factory.assert_not_called()
        self.version.assert_not_called()

    def test_demo_credentials_required_without_legacy_fallback(self):
        for env in ({}, {'BINANCE_API_KEY': 'legacy', 'BINANCE_API_SECRET': 'legacy'},
                    {**self.env, 'BINANCE_DEMO_API_KEY': ''}, {**self.env, 'BINANCE_DEMO_API_SECRET': '  '}):
            code, text = self.run_fake(environ=env)
            self.assertEqual(code, 1)
            self.assertIn('FAIL credentials', text)
        self.factory.assert_not_called()

    def test_pytest_environment_refuses_even_explicit_flag(self):
        out = io.StringIO()
        code = contract.main(['--live-read-only'], environ={**self.env, 'PYTEST_CURRENT_TEST': 'fake'},
                             adapter_factory=self.factory, version_reader=self.version, output=out)
        self.assertEqual(code, 1)
        self.assertIn('PYTEST_EXECUTION_REFUSED', out.getvalue())
        self.factory.assert_not_called()

    def test_pytest_module_refuses_even_without_environment_marker(self):
        with patch.dict(sys.modules, {'pytest': Mock()}):
            code = contract.main(['--live-read-only'], environ=self.env, adapter_factory=self.factory,
                                 version_reader=self.version, output=io.StringIO())
        self.assertEqual(code, 1)
        self.factory.assert_not_called()

    def test_version_mismatch_refuses_before_adapter(self):
        self.version.return_value = 'other-version'
        code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('ENVIRONMENT_OR_CONFIGURATION', text)
        self.factory.assert_not_called()

    def test_missing_sdk_is_sanitized(self):
        self.version.side_effect = ImportError('synthetic-demo-secret')
        code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('SDK_UNAVAILABLE', text)
        self.assertNotIn('synthetic-demo-secret', text)

    def test_production_legacy_and_wrong_host_refused_before_reads(self):
        for host in ('https://fapi.binance.com/fapi', 'https://testnet.binancefuture.com/fapi',
                     'http://demo-fapi.binance.com/fapi'):
            self.sdk.base = host
            code, text = self.run_fake()
            self.assertEqual(code, 1)
            self.assertIn('FAIL initialization', text)
        self.assertEqual(self.sdk.calls, [])

    def test_empty_orders_skip_all_lookups_and_overall_partial(self):
        code, text = self.run_fake()
        self.assertEqual(code, 2)
        self.assertEqual(text.count('NO_EXISTING_ORDER'), 4)
        self.assertIn('OVERALL PARTIAL', text)
        self.assertIn('resolved Futures host=https://demo-fapi.binance.com', text)
        self.assertIn('SDK=1.0.37', text)
        for symbol in contract.SYMBOLS:
            self.assertIn('PASS ' + symbol + ' filters', text)
            self.assertIn('PASS ' + symbol + ' fills', text)
        self.assertNotIn('futures_get_order', [name for name, _ in self.sdk.calls])
        self.assertNotIn('futures_get_algo_order', [name for name, _ in self.sdk.calls])

    def existing_orders(self):
        self.sdk.responses['futures_get_open_orders'] = [fixtures.ordinary()]
        self.sdk.responses['futures_get_open_algo_orders'] = [fixtures.algo()]
        self.sdk.responses['futures_get_order'] = fixtures.ordinary()
        self.sdk.responses['futures_get_algo_order'] = fixtures.algo()

    def test_existing_orders_lookup_both_id_forms_and_overall_pass(self):
        self.existing_orders()
        code, text = self.run_fake()
        self.assertEqual(code, 0)
        self.assertIn('OVERALL PASS', text)
        self.assertEqual(sum(name == 'futures_get_order' for name, _ in self.sdk.calls), 2)
        self.assertEqual(sum(name == 'futures_get_algo_order' for name, _ in self.sdk.calls), 2)
        self.assertNotIn('ordinary-123', text)
        self.assertNotIn('algo-123', text)
        self.assertNotIn('60000', text)
        self.assertIn('raw fields', text)

    def test_only_one_representative_per_family_is_queried(self):
        self.existing_orders()
        self.sdk.responses['futures_get_open_orders'].append(fixtures.ordinary(orderId=456, clientOrderId='other'))
        code, _ = self.run_fake()
        self.assertEqual(code, 0)
        self.assertEqual(sum(name == 'futures_get_order' for name, _ in self.sdk.calls), 2)

    def test_critical_failure_stops_remaining_reads(self):
        for error in (AuthenticationError('synthetic-demo-secret'), ConfigurationError('secret'),
                      AccountModeError('secret'), UnknownExchangeStateError('secret'), RuntimeError('secret')):
            self.sdk.calls.clear()
            with patch.object(self.adapter, 'get_account_info', side_effect=error):
                code, text = self.run_fake()
            self.assertEqual(code, 1)
            self.assertIn('OVERALL FAIL', text)
            self.assertIn('STOPPED_AFTER_FAILURE', text)
            self.assertNotIn('synthetic-demo-secret', text)
            self.assertEqual([n for n, _ in self.sdk.calls], ['futures_time', 'futures_exchange_info'])

    def test_transient_failures_stop_without_retries_and_report_partial(self):
        for kind in (ReadTimeoutError, RateLimitError, TransientReadError):
            with patch.object(self.adapter, 'get_server_time', side_effect=kind('secret')) as read:
                code, text = self.run_fake()
                read.assert_called_once()
            self.assertEqual(code, 2)
            self.assertIn('FAIL server time', text)
            self.assertIn('OVERALL PARTIAL', text)
        self.assertEqual(self.sdk.calls, [])

    def test_nested_wrong_typed_financial_model_fails(self):
        valid = self.adapter.get_account_info()
        self.sdk.calls.clear()
        with patch.object(self.adapter, 'get_account_info', return_value=replace(valid, wallet_balance=1.0)):
            code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('MALFORMED_OR_UNKNOWN_STATE', text)
        self.assertNotIn('futures_account_balance', [n for n, _ in self.sdk.calls])

    def test_wrong_container_type_fails(self):
        with patch.object(self.adapter, 'get_balances', return_value=[]):
            code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('FAIL balances', text)

    def test_account_mode_stops_before_positions(self):
        self.sdk.responses['futures_get_position_mode'] = {'dualSidePosition': True}
        code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('UNSUPPORTED_ACCOUNT_MODE', text)
        self.assertNotIn('futures_position_information', [n for n, _ in self.sdk.calls])

    def test_inconsistent_positions_stop_before_order_reads(self):
        self.sdk.responses['futures_position_information'] = [fixtures.position(), fixtures.position('ETHUSDT')]
        code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('FAIL positions', text)
        self.assertNotIn('futures_get_open_orders', [n for n, _ in self.sdk.calls])

    def test_lookup_race_or_identity_mismatch_fails_closed(self):
        self.existing_orders()
        self.sdk.responses['futures_get_order'] = fixtures.ordinary(orderId=456)
        code, text = self.run_fake()
        self.assertEqual(code, 1)
        self.assertIn('FAIL ordinary lookup exchange ID', text)
        self.assertNotIn('futures_get_algo_order', [n for n, _ in self.sdk.calls])

    def test_output_redacts_credentials_even_if_they_match_public_time(self):
        self.env['BINANCE_DEMO_API_SECRET'] = '100'
        code, text = self.run_fake()
        self.assertEqual(code, 2)
        self.assertNotIn('100', text)
        self.assertIn('[REDACTED]', text)

    def test_read_calls_are_limited_to_adapter_whitelist(self):
        self.existing_orders()
        self.run_fake()
        allowed = {'futures_time', 'futures_exchange_info', 'futures_account', 'futures_account_balance',
                   'futures_get_position_mode', 'futures_position_information', 'futures_get_open_orders',
                   'futures_get_open_algo_orders', 'futures_account_trades', 'futures_get_order', 'futures_get_algo_order'}
        self.assertEqual({n for n, _ in self.sdk.calls}, allowed)
