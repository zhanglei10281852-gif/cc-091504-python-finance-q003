from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import TrancheStatus  # noqa: E402
from equity_platform.vesting import compute_vest_date, service_days_between  # noqa: E402
from helpers import D, VEST_1, dt, make_platform  # noqa: E402


class ServiceVestingTest(unittest.TestCase):
    def test_vest_date_without_leave(self):
        platform = make_platform()
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(VEST_1, platform.tranches["G1:1"].vest_date)
        self.assertEqual(TrancheStatus.RELEASABLE, platform.tranches["G1:1"].status)

    def test_unpaid_leave_suspends_clock_and_shifts_vest_date(self):
        platform = make_platform()
        # 停薪休假 30 天：归属日顺延 30 天
        platform.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31))
        platform.recalculate("G1", dt(2025, 2, 14, 2, 0))
        tranche = platform.tranches["G1:1"]
        self.assertEqual(date(2025, 2, 13), tranche.vest_date)
        self.assertEqual(TrancheStatus.RELEASABLE, tranche.status)
        # 顺延前仍未归属
        platform2 = make_platform()
        platform2.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31))
        platform2.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(TrancheStatus.PENDING, platform2.tranches["G1:1"].status)

    def test_paid_leave_does_not_suspend_clock(self):
        platform = make_platform()
        platform.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31), suspends_vesting=False)
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(VEST_1, platform.tranches["G1:1"].vest_date)

    def test_service_days_exclude_suspending_leave(self):
        platform = make_platform()
        platform.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31))
        leaves = platform.leaves["E1"]
        self.assertEqual(365 - 30, service_days_between(date(2024, 1, 15), date(2025, 1, 14), leaves))
        self.assertEqual(date(2025, 2, 13), compute_vest_date(date(2024, 1, 15), 365, leaves))

    def test_leave_shift_cascades_over_overlapping_leave(self):
        # 顺延后的归属日落入另一段休假时继续顺延
        platform = make_platform()
        platform.add_leave("E1", "L1", date(2024, 3, 1), date(2024, 3, 31))
        platform.add_leave("E1", "L2", date(2025, 2, 10), date(2025, 2, 20))
        leaves = platform.leaves["E1"]
        self.assertEqual(date(2025, 2, 23), compute_vest_date(date(2024, 1, 15), 365, leaves))


class PerformanceVestingTest(unittest.TestCase):
    def test_partial_achievement_vests_partial_shares(self):
        platform = make_platform(performance=True)
        platform.record_performance_actual("G1", 1, D("850"))  # 达成率 85%
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        tranche = platform.tranches["G1:1"]
        self.assertEqual(TrancheStatus.RELEASABLE, tranche.status)
        self.assertEqual(D("0.85"), tranche.performance_factor)
        self.assertEqual(D("85.00"), tranche.vested_shares)
        self.assertEqual(D("15.00"), tranche.forfeited_shares)

    def test_below_threshold_forfeits_entire_tranche(self):
        platform = make_platform(performance=True)
        platform.record_performance_actual("G1", 1, D("0"))
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(TrancheStatus.FORFEITED, platform.tranches["G1:1"].status)

    def test_unresolved_performance_keeps_tranche_pending(self):
        platform = make_platform(performance=True)
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(TrancheStatus.PENDING, platform.tranches["G1:1"].status)


class BlackoutTest(unittest.TestCase):
    def test_vesting_during_blackout_locks_tranche(self):
        platform = make_platform()
        platform.add_blackout("B1", date(2025, 1, 10), date(2025, 1, 20), "财报静默期")
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        self.assertEqual(TrancheStatus.LOCKED, platform.tranches["G1:1"].status)

    def test_tranche_unlocks_after_blackout(self):
        platform = make_platform()
        platform.add_blackout("B1", date(2025, 1, 10), date(2025, 1, 20))
        platform.recalculate("G1", dt(2025, 1, 14, 2, 0))
        platform.recalculate("G1", dt(2025, 1, 21, 2, 0))
        self.assertEqual(TrancheStatus.RELEASABLE, platform.tranches["G1:1"].status)


if __name__ == "__main__":
    unittest.main()
