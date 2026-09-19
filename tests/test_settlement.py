from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import SettlementMethod, compute_settlement  # noqa: E402
from equity_platform.errors import SettlementInputError  # noqa: E402
from helpers import D  # noqa: E402


def quote(**overrides):
    params = dict(
        gross_shares=D("100"),
        price=D("50"),
        price_currency="USD",
        tax_currency="USD",
        fx_rate=None,
        withholding_rate=D("0.35"),
        method=SettlementMethod.NET_SHARE,
    )
    params.update(overrides)
    return compute_settlement(**params)


class NetShareSettlementTest(unittest.TestCase):
    def test_exact_whole_share_withholding(self):
        q = quote()
        self.assertEqual(D("5000.00"), q.gross_value)
        self.assertEqual(D("1750.00"), q.tax_due_tax_ccy)
        self.assertEqual(35, q.shares_for_tax)
        self.assertEqual(D("0.00"), q.cash_residual_to_employee)
        self.assertEqual(65, q.delivered_shares)
        self.assertEqual(D("0"), q.fractional_shares)
        self.assertEqual(D("0.00"), q.cash_in_lieu)

    def test_fractional_shares_paid_in_cash(self):
        # 绩效 85.5%：应归属 85.5 股
        q = quote(gross_shares=D("85.5"))
        self.assertEqual(D("4275.00"), q.gross_value)
        self.assertEqual(D("1496.25"), q.tax_due_tax_ccy)
        self.assertEqual(30, q.shares_for_tax)  # ceil(1496.25 / 50)
        self.assertEqual(D("1500.00"), q.tax_cover_value)
        self.assertEqual(D("3.75"), q.cash_residual_to_employee)  # 多抵部分返还
        self.assertEqual(D("55.5"), q.net_shares_exact)
        self.assertEqual(55, q.delivered_shares)
        self.assertEqual(D("0.5"), q.fractional_shares)
        self.assertEqual(D("25.00"), q.cash_in_lieu)  # 不足一股现金补偿
        self.assertEqual(D("28.75"), q.total_cash_to_employee)

    def test_sell_to_cover_records_method(self):
        q = quote(method=SettlementMethod.SELL_TO_COVER)
        self.assertEqual(SettlementMethod.SELL_TO_COVER, q.method)
        self.assertEqual(35, q.shares_for_tax)
        self.assertEqual(65, q.delivered_shares)

    def test_gross_settlement_collects_tax_in_cash(self):
        q = quote(method=SettlementMethod.GROSS)
        self.assertEqual(0, q.shares_for_tax)
        self.assertEqual(100, q.delivered_shares)
        self.assertEqual(D("1750.00"), q.cash_tax_due_from_employee)


class CrossBorderTaxTest(unittest.TestCase):
    def test_fx_conversion_for_foreign_tax_resident(self):
        # 美元股票、中国税务居民：100 股 @50 美元，汇率 7.1，预扣 45%
        q = quote(tax_currency="CNY", fx_rate=D("7.1"), withholding_rate=D("0.45"))
        self.assertEqual(D("5000.00"), q.gross_value)
        self.assertEqual(D("35500.00"), q.taxable_income_tax_ccy)
        self.assertEqual(D("15975.00"), q.tax_due_tax_ccy)
        self.assertEqual(D("2250.00"), q.tax_due_price_ccy)
        self.assertEqual(45, q.shares_for_tax)
        self.assertEqual(55, q.delivered_shares)

    def test_missing_fx_rate_rejected(self):
        with self.assertRaises(SettlementInputError):
            quote(tax_currency="CNY", fx_rate=None)

    def test_tax_exceeding_share_value_falls_back_to_cash(self):
        q = quote(withholding_rate=D("1.5"))
        self.assertEqual(100, q.shares_for_tax)  # 全部股份抵税
        self.assertEqual(D("2500.00"), q.cash_tax_due_from_employee)  # 差额现金补缴
        self.assertEqual(0, q.delivered_shares)


class SettlementInputTest(unittest.TestCase):
    def test_invalid_inputs_rejected(self):
        with self.assertRaises(SettlementInputError):
            quote(gross_shares=D("0"))
        with self.assertRaises(SettlementInputError):
            quote(price=D("0"))
        with self.assertRaises(SettlementInputError):
            quote(withholding_rate=D("-0.1"))

    def test_deterministic_for_same_inputs(self):
        self.assertEqual(quote(gross_shares=D("85.5")), quote(gross_shares=D("85.5")))


if __name__ == "__main__":
    unittest.main()
