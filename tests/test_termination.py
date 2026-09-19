from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import TerminationType, TrancheStatus  # noqa: E402
from equity_platform.reports import post_termination_tranches  # noqa: E402
from helpers import D, dt, make_platform, vested_platform  # noqa: E402


class VoluntaryTerminationTest(unittest.TestCase):
    def test_unvested_forfeited_and_vested_gets_window(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        self.assertEqual(TrancheStatus.RELEASABLE, platform.tranches["G1:1"].status)
        self.assertEqual(date(2025, 4, 20), platform.tranches["G1:1"].settlement_deadline)
        for seq in (2, 3, 4):
            self.assertEqual(TrancheStatus.FORFEITED, platform.tranches[f"G1:{seq}"].status)

    def test_settlement_within_window_succeeds(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        records = platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        self.assertEqual(65, records[0].quote.delivered_shares)

    def test_window_expiry_forfeits_tranche(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        expired = platform.expire_windows(dt(2025, 4, 21, 0, 0))
        self.assertEqual(["G1:1"], [t.tranche_id for t in expired])
        self.assertEqual(TrancheStatus.FORFEITED, platform.tranches["G1:1"].status)

    def test_window_not_expired_before_deadline(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        self.assertEqual([], platform.expire_windows(dt(2025, 4, 20, 15, 59, 59)))


class OtherTerminationTypesTest(unittest.TestCase):
    def test_for_cause_forfeits_vested_unsettled(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.FOR_CAUSE, date(2025, 1, 20), at=as_of)
        self.assertEqual(TrancheStatus.FORFEITED, platform.tranches["G1:1"].status)

    def test_death_disability_accelerates_unvested(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.DEATH_DISABILITY, date(2025, 1, 20), at=as_of)
        tranche = platform.tranches["G1:2"]
        self.assertEqual(TrancheStatus.RELEASABLE, tranche.status)
        self.assertEqual(D("100"), tranche.vested_shares)
        self.assertEqual(date(2026, 1, 20), tranche.settlement_deadline)

    def test_acceleration_with_unresolved_performance_forfeits(self):
        platform = make_platform(performance=True)
        as_of = dt(2025, 1, 14, 2, 0)
        platform.terminate("E1", TerminationType.DEATH_DISABILITY, date(2025, 1, 20), at=as_of)
        self.assertEqual(TrancheStatus.FORFEITED, platform.tranches["G1:1"].status)


class PostTerminationReportTest(unittest.TestCase):
    def test_report_lists_open_tranches_with_deadline(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        items = post_termination_tranches(platform, dt(2025, 2, 1, 0, 0))
        self.assertEqual(1, len(items))
        self.assertEqual("G1:1", items[0]["tranche_id"])
        self.assertEqual(date(2025, 4, 20), items[0]["settlement_deadline"])
        self.assertEqual(78, items[0]["days_remaining"])

    def test_report_empty_after_settlement(self):
        platform, as_of = vested_platform()
        platform.terminate("E1", TerminationType.VOLUNTARY, date(2025, 1, 20), at=as_of)
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        self.assertEqual([], post_termination_tranches(platform, as_of))


if __name__ == "__main__":
    unittest.main()
