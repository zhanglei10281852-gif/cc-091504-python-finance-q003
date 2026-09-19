from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform.reports import (  # noqa: E402
    employee_statement,
    failed_receipts,
    pending_approvals,
)
from helpers import D, VEST_1, dt, vested_platform  # noqa: E402


def settle_first_tranche(platform, as_of):
    platform.create_batch("B1", ["G1:1"], at=as_of)
    platform.submit_batch("B1")
    for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
        platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
    return platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)


class EmployeeStatementTest(unittest.TestCase):
    def test_statement_covers_conditions_tax_fx_and_payout(self):
        platform, as_of = vested_platform()
        # 跨境：中国税务居民，人民币计税，汇率 7.1
        platform.set_tax_identity("E1", "CN", D("0.45"), "CNY")
        platform.add_fx_rate("USD", "CNY", VEST_1, D("7.1"))
        settle_first_tranche(platform, as_of)

        statement = employee_statement(platform, "E1", as_of)
        self.assertEqual(4, len(statement["tranches"]))
        first = statement["tranches"][0]

        service = first["service"]
        self.assertEqual(365, service["required_service_days"])
        self.assertEqual(365, service["credited_service_days"])
        self.assertEqual(0, service["leave_days_deducted"])
        self.assertEqual(date(2025, 1, 14), service["vest_date"])

        settlement = first["settlement"]
        self.assertEqual("net_share", settlement["method"])
        self.assertEqual(D("50"), settlement["price"])
        self.assertEqual("USD", settlement["price_currency"])
        self.assertEqual(D("7.1"), settlement["fx_rate"])
        self.assertEqual("CNY", settlement["tax_currency"])
        self.assertEqual(D("15975.00"), settlement["tax_due_tax_ccy"])
        self.assertEqual(45, settlement["shares_for_tax"])
        self.assertEqual(55, settlement["delivered_shares"])
        self.assertEqual("k1:G1:1", settlement["idempotency_key"])
        self.assertTrue(settlement["ledger_entry_ids"])

        # 未归属批次仍展示待满足状态与计划归属日
        second = statement["tranches"][1]
        self.assertEqual("PENDING", second["status"])
        self.assertIsNone(second["settlement"])

        # 状态流转留痕可查
        transitions = [(e["from"], e["to"]) for e in first["history"]]
        self.assertIn(("PENDING", "RELEASABLE"), transitions)
        self.assertIn(("RELEASABLE", "SETTLED"), transitions)

    def test_statement_shows_leave_adjustment(self):
        platform, as_of = vested_platform()
        platform.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31))
        statement = employee_statement(platform, "E1", as_of)
        self.assertEqual(30, statement["tranches"][0]["service"]["leave_days_deducted"])


class AdminViewsTest(unittest.TestCase):
    def test_pending_approvals_lists_missing_roles(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        platform.review_batch("B1", role="HR", decision="APPROVED", at=as_of)
        pending = pending_approvals(platform)
        self.assertEqual(1, len(pending))
        self.assertEqual(["PAYROLL", "STOCK_ADMIN"], pending[0]["missing_roles"])
        # 三方确认完成后不再出现
        for role in ("PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        self.assertEqual([], pending_approvals(platform))

    def test_failed_receipts_tracked_until_retried_ok(self):
        platform, as_of = vested_platform()
        settle_first_tranche(platform, as_of)
        platform.record_receipt("G1:1", "broker", False, "券商拒绝：账户冻结", at=as_of)
        failed = failed_receipts(platform)
        self.assertEqual(1, len(failed))
        self.assertEqual("broker", failed[0]["channel"])
        # 重发成功回执后消除
        platform.record_receipt("G1:1", "broker", True, "已交割", at=as_of)
        self.assertEqual([], failed_receipts(platform))

    def test_receipt_requires_settled_tranche(self):
        platform, as_of = vested_platform()
        from equity_platform.errors import WorkflowError

        with self.assertRaises(WorkflowError):
            platform.record_receipt("G1:1", "broker", True, at=as_of)


if __name__ == "__main__":
    unittest.main()
