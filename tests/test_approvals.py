from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import BatchStatus, FieldDiff, TrancheStatus  # noqa: E402
from equity_platform.errors import WorkflowError  # noqa: E402
from helpers import D, dt, vested_platform  # noqa: E402


def approved_batch(platform, as_of, batch_id="B1", tranches=("G1:1",)):
    platform.create_batch(batch_id, list(tranches), at=as_of)
    platform.submit_batch(batch_id)
    for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
        platform.review_batch(batch_id, role=role, decision="APPROVED", at=as_of)
    return platform.batches[batch_id]


class ApprovalWorkflowTest(unittest.TestCase):
    def test_execute_requires_all_three_roles(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        platform.review_batch("B1", role="HR", decision="APPROVED", at=as_of)
        platform.review_batch("B1", role="PAYROLL", decision="APPROVED", at=as_of)
        with self.assertRaises(WorkflowError):
            platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        platform.review_batch("B1", role="STOCK_ADMIN", decision="APPROVED", at=as_of)
        records = platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        self.assertEqual(1, len(records))
        self.assertEqual(TrancheStatus.SETTLED, platform.tranches["G1:1"].status)

    def test_rejection_preserves_opinion_and_differences(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        platform.review_batch("B1", role="HR", decision="APPROVED", at=as_of)
        platform.review_batch(
            "B1",
            role="PAYROLL",
            decision="REJECTED",
            comment="预扣税率与最新税务身份不符",
            differences=[FieldDiff("withholding_rate", "0.35", "0.45")],
            at=as_of,
        )
        batch = platform.batches["B1"]
        self.assertEqual(BatchStatus.RETURNED, batch.status)
        self.assertEqual(2, len(batch.reviews))
        rejection = batch.reviews[-1]
        self.assertEqual("预扣税率与最新税务身份不符", rejection.comment)
        self.assertEqual("0.45", rejection.differences[0].expected)
        with self.assertRaises(WorkflowError):
            platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)

    def test_resubmit_bumps_version_and_keeps_history(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        platform.review_batch("B1", role="HR", decision="APPROVED", at=as_of)
        platform.review_batch("B1", role="PAYROLL", decision="REJECTED", comment="税率有误", at=as_of)
        # 薪资岗修正数据后重新提交
        platform.set_tax_identity("E1", "CN", D("0.45"), "USD")
        platform.resubmit_batch("B1")
        batch = platform.batches["B1"]
        self.assertEqual(2, batch.version)
        self.assertEqual(BatchStatus.IN_REVIEW, batch.status)
        # 旧版本的同意不计入新版本
        self.assertEqual({"HR", "PAYROLL", "STOCK_ADMIN"}, {r.value for r in batch.missing_roles()})
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        self.assertEqual(BatchStatus.APPROVED, batch.status)
        # 历史意见完整保留：v1 两条 + v2 三条
        self.assertEqual(5, len(batch.reviews))
        self.assertEqual("税率有误", batch.reviews[1].comment)
        records = platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        self.assertEqual(D("0.45"), records[0].quote.withholding_rate)

    def test_review_only_allowed_in_review_state(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        with self.assertRaises(WorkflowError):
            platform.review_batch("B1", role="HR", decision="APPROVED", at=as_of)

    def test_blackout_blocks_execution(self):
        platform, as_of = vested_platform()
        from datetime import date

        platform.add_blackout("BW", date(2025, 1, 14), date(2025, 1, 20))
        platform.recalculate("G1", as_of)  # 转入锁定
        self.assertEqual(TrancheStatus.LOCKED, platform.tranches["G1:1"].status)
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        with self.assertRaises(WorkflowError):
            platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)


if __name__ == "__main__":
    unittest.main()
