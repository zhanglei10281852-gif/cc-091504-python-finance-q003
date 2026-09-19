from __future__ import annotations

import sys
import unittest
from datetime import timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import CNY_BRACKETS, GRANT, at, make_grant, make_service, tranche

from equity.enums import SettlementMode, TrancheState
from equity.settlement import compute_settlement
from equity.tax import progressive_tax
from equity.models import TaxBracket

UTC = timezone.utc


def settle_kwargs(**over):
    base = dict(
        mode=SettlementMode.NET_SHARE,
        gross_shares=Decimal("1000"),
        price=Decimal("50"),
        price_currency="USD",
        price_at=at(2024, 1, 1),
        fx_rate=Decimal("7.0"),
        fx_at=at(2024, 1, 1),
        tax_currency="CNY",
        brackets=CNY_BRACKETS,
    )
    base.update(over)
    return base


class ProgressiveTaxTest(unittest.TestCase):
    def test_tiered(self):
        # 50000:36000*3% + 14000*10% = 1080 + 1400 = 2480
        tax = progressive_tax(Decimal("50000"), CNY_BRACKETS)
        self.assertEqual(Decimal("2480"), tax)

    def test_zero_income(self):
        self.assertEqual(Decimal("0"), progressive_tax(Decimal("0"), CNY_BRACKETS))


class NetShareTest(unittest.TestCase):
    def test_exact_cover(self):
        # 应纳税所得额 1000*50*7 = 350000 CNY
        # 税 = 36000*3% + 108000*10% + 156000*20% + 50000*25% = 1080+10800+31200+12500 = 55580
        # 折美元 55580/7 = 7940 -> 7940/50 = 158.8 -> 扣 159 股,多扣 0.2 股*50*7 = 70 CNY 退还
        b = compute_settlement(**settle_kwargs())
        self.assertEqual(Decimal("350000.00"), b.taxable_income_tax_ccy)
        self.assertEqual(Decimal("55580.00"), b.tax_due_tax_ccy)
        self.assertEqual(Decimal("159"), b.shares_for_tax)
        self.assertEqual(Decimal("841"), b.net_shares)
        self.assertEqual(Decimal("70.00"), b.withhold_refund_tax_ccy)

    def test_fractional_cash_in_lieu(self):
        b = compute_settlement(**settle_kwargs(gross_shares=Decimal("1000.5")))
        self.assertEqual(Decimal("1000"), b.whole_shares)
        self.assertEqual(Decimal("0.5"), b.fractional_shares)
        self.assertEqual(Decimal("175.00"), b.fractional_cash_tax_ccy)  # 0.5*50*7

    def test_same_currency_no_fx(self):
        b = compute_settlement(
            **settle_kwargs(
                fx_rate=None, fx_at=None, tax_currency="USD",
                brackets=(TaxBracket(Decimal("0"), Decimal("0.10")),),
            )
        )
        self.assertIsNone(b.fx_rate)
        self.assertEqual(Decimal("5000.00"), b.tax_due_tax_ccy)  # 50000*10%
        self.assertEqual(Decimal("100"), b.shares_for_tax)

    def test_tax_exceeds_value_employee_owes(self):
        # 收入 10*50*7=3500,税率 95% -> 税 3325 CNY = 475 USD -> 9.5 股进整 10 股
        b = compute_settlement(
            **settle_kwargs(
                gross_shares=Decimal("10"),
                brackets=(TaxBracket(Decimal("0"), Decimal("0.95")),),
            )
        )
        self.assertEqual(Decimal("10"), b.shares_for_tax)
        self.assertEqual(Decimal("0"), b.net_shares)
        self.assertEqual(Decimal("0.00"), b.employee_cash_due_tax_ccy)
        # 极端:税率 200% -> 税 7000 CNY = 1000 USD,需 20 股 > 整股 10,差额现金补缴
        b2 = compute_settlement(
            **settle_kwargs(
                gross_shares=Decimal("10"),
                brackets=(TaxBracket(Decimal("0"), Decimal("2.0")),),
            )
        )
        self.assertEqual(Decimal("10"), b2.shares_for_tax)
        self.assertEqual(Decimal("3500.00"), b2.employee_cash_due_tax_ccy)
        self.assertEqual(Decimal("0"), b2.net_shares)


class SellToCoverTest(unittest.TestCase):
    def test_residual_returned(self):
        b = compute_settlement(**settle_kwargs(mode=SettlementMode.SELL_TO_COVER))
        # 卖 159 股得 159*50*7 = 55650,税 55580,剩余 70 返还
        self.assertEqual(Decimal("159"), b.shares_for_tax)
        self.assertEqual(Decimal("70.00"), b.sell_residual_tax_ccy)
        self.assertEqual(Decimal("841"), b.net_shares)


class GrossModeTest(unittest.TestCase):
    def test_no_share_withholding(self):
        b = compute_settlement(**settle_kwargs(mode=SettlementMode.GROSS))
        self.assertEqual(Decimal("0"), b.shares_for_tax)
        self.assertEqual(Decimal("1000"), b.net_shares)
        self.assertEqual(Decimal("55580.00"), b.payroll_withholding_tax_ccy)


class ServiceSettlementTest(unittest.TestCase):
    """经服务层结算:状态推进、回执、台账。"""

    def _ready(self, mode=SettlementMode.NET_SHARE):
        service = make_service()
        make_grant(service, mode=mode)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))
        helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
        return service

    def test_settle_flow(self):
        service = self._ready()
        receipt = service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))
        self.assertEqual("COMPLETED", receipt.status.value)
        self.assertEqual(TrancheState.SETTLED, tranche(service, "T1").state)
        self.assertEqual(Decimal("841"), receipt.breakdown.net_shares)
        self.assertTrue(receipt.confirmation_no)
        # 台账:股份出库 1000,税 55580,现金付出 70
        entries = service.ledger_entries(GRANT)
        kinds = {e.kind: e.quantity for e in entries}
        self.assertEqual(Decimal("1000"), kinds["SHARE_DEBIT"])
        self.assertEqual(Decimal("55580.00"), kinds["TAX_REMITTANCE"])
        self.assertEqual(Decimal("70.00"), kinds["CASH_PAYOUT"])

    def test_idempotent_replay(self):
        service = self._ready()
        r1 = service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))
        r2 = service.settle("T1", idempotency_key="K1", now=at(2024, 1, 5))
        self.assertEqual(r1.receipt_id, r2.receipt_id)
        self.assertEqual(Decimal("1000"), service.shares_debited("T1"))

    def test_different_key_after_settled_conflicts(self):
        service = self._ready()
        service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))
        from equity.errors import ConflictError

        with self.assertRaises(ConflictError) as ctx:
            service.settle("T1", idempotency_key="K2", now=at(2024, 1, 5))
        self.assertEqual("already_settled", ctx.exception.code)
        self.assertEqual(Decimal("1000"), service.shares_debited("T1"))

    def test_settle_requires_exercisable(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))  # LOCKED,未审批
        from equity.errors import ConflictError

        with self.assertRaises(ConflictError):
            service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))


if __name__ == "__main__":
    unittest.main()
