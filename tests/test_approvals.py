from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import GRANT, at, make_grant, make_service, tranche

from equity.enums import ReviewDecision, Role, TrancheState
from equity.errors import ValidationError
from equity.models import LeavePeriod
from equity.reports import admin_dashboard


def locked_service():
    service = make_service()
    make_grant(service)
    service.evaluate_grant(GRANT, now=at(2024, 1, 2))  # T1 -> LOCKED
    return service


class ApprovalFlowTest(unittest.TestCase):
    def test_three_roles_confirm_then_exercisable(self):
        service = locked_service()
        for role in (Role.HR, Role.PAYROLL):
            service.review("T1", role=role, decision=ReviewDecision.CONFIRMED, opinion="ok", now=at(2024, 1, 3))
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T1").state)
        service.review("T1", role=Role.SECURITIES, decision=ReviewDecision.CONFIRMED, opinion="ok", now=at(2024, 1, 3))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)

    def test_return_requires_opinion(self):
        service = locked_service()
        with self.assertRaises(ValidationError) as ctx:
            service.review("T1", role=Role.HR, decision=ReviewDecision.RETURNED, opinion=" ", now=at(2024, 1, 3))
        self.assertEqual("opinion_required", ctx.exception.code)

    def test_return_keeps_opinion_and_diff_on_resubmit(self):
        service = locked_service()
        # 人事退回:休假天数与系统记录不符
        service.review(
            "T1", role=Role.HR, decision=ReviewDecision.RETURNED,
            opinion="缺少 2024-03 停薪休假记录", now=at(2024, 1, 3),
        )
        # 修正数据:补登休假
        service.record_leave(GRANT, LeavePeriod(date(2024, 3, 1), date(2024, 3, 10)), now=at(2024, 1, 4))
        record = service.review(
            "T1", role=Role.HR, decision=ReviewDecision.CONFIRMED, opinion="已补录,确认", now=at(2024, 1, 5),
        )
        case = service.store.state.approval_cases["T1"]
        # 原退回意见保留
        returned = [r for r in case.reviews if r.decision == ReviewDecision.RETURNED]
        self.assertEqual(1, len(returned))
        self.assertEqual("缺少 2024-03 停薪休假记录", returned[0].opinion)
        # 重新确认的记录携带差异
        self.assertIsNotNone(record.diff_from_previous)
        self.assertIn("leaves", record.diff_from_previous)

    def test_data_change_invalidates_confirmation(self):
        service = locked_service()
        helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)
        # 人事数据变更(补登休假)-> HR 确认失效,退回 LOCKED
        service.record_leave(GRANT, LeavePeriod(date(2024, 3, 1), date(2024, 3, 5)), now=at(2024, 1, 4))
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T1").state)
        dashboard = admin_dashboard(service)
        pending = [p for p in dashboard["待完成审批"] if p["tranche_id"] == "T1"]
        self.assertEqual(1, len(pending))
        self.assertIn("HR", pending[0]["待确认岗位"])

    def test_review_before_vesting_rejected(self):
        service = make_service()
        make_grant(service)
        with self.assertRaises(ValidationError) as ctx:
            service.review("T1", role=Role.HR, decision=ReviewDecision.CONFIRMED, now=at(2023, 6, 1))
        self.assertEqual("not_ready_for_review", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
