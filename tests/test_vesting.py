from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import GRANT, at, make_grant, make_service, tranche

from equity.enums import TerminationType, TrancheState
from equity.models import BlackoutWindow, LeavePeriod, Termination
from equity.vesting import adjusted_vest_date, roll_out_of_blackout

UTC = timezone.utc


class LeavePauseTest(unittest.TestCase):
    """停薪休假暂停计时:实际归属日后移。"""

    def test_leave_pushes_vest_date(self):
        service = make_service()
        make_grant(service)
        service.record_leave(
            GRANT, LeavePeriod(date(2024, 6, 1), date(2024, 6, 30), "停薪留职"), now=at(2024, 6, 1)
        )
        # T2 基准 2025-01-01,此前有 30 天休假 -> 调整后 2025-01-31
        service.evaluate_grant(GRANT, now=at(2024, 7, 1))
        self.assertEqual(date(2025, 1, 31), tranche(service, "T2").adjusted_vest_date)

    def test_chained_leaves_converge(self):
        # 第一段休假把归属日推过第二段休假的起点,两段都要计入(不动点)
        leaves = [
            LeavePeriod(date(2024, 12, 20), date(2024, 12, 31)),  # 12 天
            LeavePeriod(date(2025, 1, 5), date(2025, 1, 10)),     # 6 天
        ]
        adjusted = adjusted_vest_date(date(2025, 1, 1), date(2023, 1, 1), leaves)
        self.assertEqual(date(2025, 1, 19), adjusted)  # 1 月 1 日 + 18 天

    def test_leave_after_termination_ignored(self):
        service = make_service()
        make_grant(service)
        service.record_leave(
            GRANT, LeavePeriod(date(2025, 6, 1), date(2025, 12, 31)), now=at(2025, 6, 1)
        )
        service.record_termination(
            GRANT,
            Termination(TerminationType.VOLUNTARY, date(2025, 7, 1)),
            now=at(2025, 7, 1),
        )
        # 休假截断到离职日:仅 6 月 30 天计入
        record = tranche(service, "T3")
        self.assertEqual(TrancheState.LAPSED, record.state)


class PerformanceTest(unittest.TestCase):
    def test_partial_achievement(self):
        service = make_service()
        make_grant(service)
        helpers.assess(service, "90")  # 达成率 0.9 -> 阶梯 0.5
        service.evaluate_grant(GRANT, now=at(2025, 1, 2))
        record = tranche(service, "T2")
        self.assertEqual(TrancheState.LOCKED, record.state)
        self.assertEqual(Decimal("0.9000"), record.achievement)
        self.assertEqual(Decimal("0.5"), record.payout)
        self.assertEqual(Decimal("500.0000"), record.eligible_shares)

    def test_full_achievement(self):
        service = make_service()
        make_grant(service)
        helpers.assess(service, "110")
        service.evaluate_grant(GRANT, now=at(2025, 1, 2))
        self.assertEqual(Decimal("1000.0000"), tranche(service, "T2").eligible_shares)

    def test_below_threshold_lapses(self):
        service = make_service()
        make_grant(service)
        helpers.assess(service, "70")  # 0.7 < 0.8 门槛
        service.evaluate_grant(GRANT, now=at(2025, 1, 2))
        record = tranche(service, "T2")
        self.assertEqual(TrancheState.LAPSED, record.state)
        self.assertIn("绩效未达标", record.lapse_reason)

    def test_unassessed_stays_pending(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2025, 1, 2))
        self.assertEqual(TrancheState.PENDING, tranche(service, "T2").state)


class TerminationPolicyTest(unittest.TestCase):
    def test_voluntary_lapses_future_keeps_vested(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))  # T1 -> LOCKED
        service.record_termination(
            GRANT, Termination(TerminationType.VOLUNTARY, date(2024, 6, 1)), now=at(2024, 6, 1)
        )
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T1").state)
        self.assertEqual(TrancheState.LAPSED, tranche(service, "T3").state)
        self.assertEqual(TrancheState.LAPSED, tranche(service, "T4").state)
        # 行权窗口 90 天:2024-06-01 + 90 = 2024-08-30,上海时区日末
        deadline = tranche(service, "T1").exercise_deadline
        self.assertEqual(datetime(2024, 8, 30, 15, 59, 59, 999999, tzinfo=UTC), deadline)

    def test_for_cause_forfeits_everything(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))
        service.record_termination(
            GRANT, Termination(TerminationType.FOR_CAUSE, date(2024, 6, 1)), now=at(2024, 6, 1)
        )
        for tid in ("T1", "T2", "T3", "T4"):
            self.assertEqual(TrancheState.LAPSED, tranche(service, tid).state)

    def test_retirement_accelerates_unvested(self):
        service = make_service()
        make_grant(service)
        helpers.assess(service, "100")
        service.record_termination(
            GRANT, Termination(TerminationType.RETIREMENT, date(2024, 6, 1)), now=at(2024, 6, 1)
        )
        # 退休 100% 加速:T2 绩效批次按评定结果归属,归属日视为离职日
        record = tranche(service, "T2")
        self.assertEqual(TrancheState.LOCKED, record.state)
        self.assertEqual(Decimal("1000.0000"), record.eligible_shares)
        self.assertEqual(date(2024, 6, 1), record.adjusted_vest_date)
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T3").state)

    def test_exercise_window_expiry_lapses(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))
        service.record_termination(
            GRANT, Termination(TerminationType.VOLUNTARY, date(2024, 6, 1)), now=at(2024, 6, 1)
        )
        service.evaluate_grant(GRANT, now=at(2024, 9, 1))  # 超过 90 天窗口
        self.assertEqual(TrancheState.LAPSED, tranche(service, "T1").state)


class BlackoutTest(unittest.TestCase):
    def _locked_tranche(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))
        return service

    def test_blackout_blocks_exercisable(self):
        service = self._locked_tranche()
        service.add_blackout(
            BlackoutWindow(at(2024, 1, 1), at(2024, 2, 1), "年报静默期")
        )
        helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
        # 审批齐全但在黑窗内,仍为 LOCKED
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T1").state)
        service.evaluate_grant(GRANT, now=at(2024, 2, 2))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)

    def test_exercisable_reverts_in_blackout(self):
        service = self._locked_tranche()
        helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)
        service.add_blackout(BlackoutWindow(at(2024, 1, 10), at(2024, 1, 20), "重大事项"))
        service.evaluate_grant(GRANT, now=at(2024, 1, 15))
        self.assertEqual(TrancheState.LOCKED, tranche(service, "T1").state)
        service.evaluate_grant(GRANT, now=at(2024, 1, 21))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)

    def test_deadline_rolls_out_of_blackout(self):
        blackouts = [
            BlackoutWindow(at(2024, 8, 28), at(2024, 9, 3), "静默期"),
            BlackoutWindow(at(2024, 9, 5), at(2024, 9, 7), "公告期"),
        ]
        deadline = datetime(2024, 8, 30, 16, 0, tzinfo=UTC)
        self.assertEqual(at(2024, 9, 3), roll_out_of_blackout(deadline, blackouts))
        # 落在两个黑窗之间则不动
        self.assertEqual(at(2024, 9, 4), roll_out_of_blackout(at(2024, 9, 4), blackouts))


if __name__ == "__main__":
    unittest.main()
