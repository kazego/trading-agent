"""Synthetic A1 compatibility vectors; no captured/live snapshot construction."""
import unittest
from dataclasses import replace
from decimal import Decimal as D

import test_paper_settlement as b3
from paper.market import FILTER_SCOPE
from paper.model import encoded, parse
from paper.operations import rules_from
from paper.settlement import settle, protect


def snapshot(low='1.00', high='1000.00', mutate=None):
    old = b3.rules()
    rows = []
    for rule in old.rules:
        rows.append(dict(symbol=rule.symbol, status='TRADING', contractType='PERPETUAL',
            quoteAsset='USDT', marginAsset='USDT', classifications=dict(FILTER_SCOPE), filters=[
                dict(filterType='PRICE_FILTER', minPrice=low, maxPrice=high, tickSize='0.01'),
                dict(filterType='LOT_SIZE', minQty='0.001', maxQty='100000', stepSize='0.001'),
                dict(filterType='MARKET_LOT_SIZE', minQty='0.001', maxQty='100000', stepSize='0.001'),
                dict(filterType='MIN_NOTIONAL', notional='5.00'),
                dict(filterType='MAX_NUM_ORDERS', limit=200),
                dict(filterType='PERCENT_PRICE', multiplierUp='0.0001', multiplierDown='99999', multiplierDecimal='4'),
                dict(filterType='POSITION_RISK_CONTROL', positionControlSide='UNINTERPRETED_SYNTHETIC'),
            ]))
    data = dict(endpoint='https://fapi.binance.com/fapi/v1/exchangeInfo',
                acquired_utc='2000-01-01T00:00:00Z', response_sha256='a'*64, symbols=rows)
    if mutate:
        mutate(data)
    return replace(old, static_provenance=encoded(data))


class RulesTests(unittest.TestCase):
    def test_exact_bounds_and_roundtrip(self):
        r = snapshot('98.02', '104.02')
        self.assertEqual(rules_from(r.record), r)
        self.assertTrue(r.prices_allowed('BTCUSDT', D('98.02'), D('104.02')))
        self.assertEqual(settle(b3.pending(r), b3.candle(b3.pending(r)), r).status, 'OPEN')

    def test_entry_below_and_above(self):
        for low, high in [('101', '1000'), ('1', '99')]:
            r = snapshot(low, high); p = b3.pending(r)
            result = settle(p, b3.candle(p), r)
            self.assertEqual((result.status, result.code), ('REJECTED_EXECUTION', 'STATIC_PRICE_BOUNDS'))

    def test_stop_target_outside(self):
        for low, high in [('99', '1000'), ('1', '103')]:
            r = snapshot(low, high); p = b3.pending(r)
            self.assertEqual(settle(p, b3.candle(p), r).status, 'REJECTED_EXECUTION')

    def test_disabled_bounds(self):
        self.assertTrue(snapshot('0', '0').prices_allowed('BTCUSDT', D('0.001'), D('999999')))

    def test_malformed_decimal_bounds(self):
        for value in ['NaN', 'Infinity', '-1', 'bad', ' 1', 1.0]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot(value)
        with self.assertRaises(ValueError): snapshot('100', '10')
        with self.assertRaises(ValueError): snapshot('0.001')

    def test_unknown_duplicate_missing_filters(self):
        mutations = [lambda d: d['symbols'][0]['filters'].append(dict(filterType='FUTURE_RULE')),
                     lambda d: d['symbols'][0]['filters'].pop(),
                     lambda d: d['symbols'][0]['filters'].__setitem__(1, d['symbols'][0]['filters'][0]),
                     lambda d: d['symbols'][0]['classifications'].__setitem__('POSITION_RISK_CONTROL', 'DISABLED')]
        for mutation in mutations:
            with self.assertRaises(ValueError): snapshot(mutate=mutation)

    def test_raw_mapping_mismatch(self):
        with self.assertRaises(ValueError):
            snapshot(mutate=lambda d: d['symbols'][0]['filters'][1].__setitem__('stepSize', '0.002'))

    def test_legacy_record_unchanged(self):
        r = b3.rules()
        self.assertNotIn('static_provenance', r.record)
        self.assertEqual(rules_from(r.record), r)
        self.assertEqual(r.record['schema'], 'paper-symbol-rules-v1')

    def test_opaque_metadata_preserved_not_enforced(self):
        r = snapshot(); raw = parse(r.static_provenance)
        self.assertEqual(raw['symbols'][0]['filters'][0]['minPrice'], '1.00')
        self.assertEqual(raw['symbols'][0]['filters'][-1]['positionControlSide'], 'UNINTERPRETED_SYNTHETIC')
        self.assertEqual(rules_from(r.record).static_provenance, r.static_provenance)
        p = b3.pending(r)
        self.assertEqual(settle(p, b3.candle(p), r).status, 'OPEN')

    def test_sizing_slippage_fees_unchanged(self):
        for signals in [('LONG',)*3, ('SHORT',)*3]:
            old, new = b3.rules(), snapshot()
            a, b = b3.pending(old, signals), b3.pending(new, signals)
            pa, pb = settle(a, b3.candle(a, open='100.003'), old).position, settle(b, b3.candle(b, open='100.003'), new).position
            for field in ('entry', 'stop_loss', 'take_profit', 'requested_risk', 'planned_stop_risk', 'utilization'):
                self.assertEqual(getattr(pa, field), getattr(pb, field))
            self.assertNotEqual(pb.entry.price % D('.01'), 0)

    def test_no_fallback(self):
        r = snapshot('101'); p = b3.pending(r)
        before = p.result
        self.assertEqual(settle(p, b3.candle(p), r).status, 'REJECTED_EXECUTION')
        self.assertEqual(p.symbol, 'BTCUSDT')
        self.assertEqual(p.result, before)
        self.assertEqual(sum(d['reason'] == 'BLOCKED_PRIORITY' for d in parse(before)['dispositions']), 2)

    def test_gap_exit_outside_bounds_unchanged(self):
        results = []
        for r in (b3.rules(), snapshot('90', '110')):
            p = b3.pending(r); c = b3.candle(p)
            pos = settle(p, c, r).position
            pos = protect(pos, c).position
            trade = protect(pos, b3.candle(p, time=b3.E+900000, open='80', low='79', high='81', close='80')).trade
            results.append((trade.exit, trade.gross_pnl, trade.net_pnl))
        self.assertEqual(*results)

    def test_unknown_schema_and_extra_fields(self):
        for change in ({'schema':'future'}, {'unexpected':1}):
            value = snapshot().record; value.update(change)
            with self.assertRaises(ValueError): rules_from(value)
