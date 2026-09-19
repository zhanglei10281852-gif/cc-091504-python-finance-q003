from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import SettlementMethod, TrancheSpec, TrancheStatus  # noqa: E402
from equity_platform.errors import AmendmentError  # noqa: E402
from helpers import D, dt, make_platform, vested_platform  # noqa: E402


def specs(shares=("100", "100", "100", "100")):
    return [TrancheSpec(seq, 365 * seq, D(s)) for seq, s in enumerate(shares, start=1)]


class AmendmentVersionTest(unittest.TestCase):
    def test_amendment_only_forms_forward_version(self):
        platform = make_platform()
        with self.assertRaises(AmendmentError):
            platform.amend_grant("G1", effective_from=date(2024, 6, 1), at=dt(2025, 1, 10, 2, 0))

    def test_new_version_applies_only_after_effective_date(self):
        platform = make_platform()
        platform.amend_grant(
            "G1",
            effective_from=date(2025, 2, 1),
            at=dt(2025, 1, 10, 2, 0),
            settlement_method=SettlementMethod.SELL_TO_COVER,
        )
        grant = platform.grants["G1"]
        self.assertEqual(2, len(grant.terms_history))
        self.assertEqual(
            SettlementMethod.NET_SHARE, grant.terms_as_of(date(2025, 1, 31)).settlement_method
        )
        self.assertEqual(
            SettlementMethod.SELL_TO_COVER, grant.terms_as_of(date(2025, 2, 1)).settlement_method
        )

    def test_pending_tranche_follows_new_version(self):
        platform = make_platform()
        platform.amend_grant(
            "G1",
            effective_from=date(2025, 1, 10),
            at=dt(2025, 1, 10, 2, 0),
            tranches=specs(("100", "120", "120", "120")),
        )
        tranche = platform.tranches["G1:2"]
        self.assertEqual(D("120"), tranche.spec_shares)
        self.assertEqual(2, tranche.terms_version)

    def test_removed_pending_tranche_is_forfeited(self):
        platform = make_platform()
        platform.amend_grant(
            "G1",
            effective_from=date(2025, 1, 10),
            at=dt(2025, 1, 10, 2, 0),
            tranches=specs(("100", "100", "100"))[:3],
        )
        self.assertEqual(TrancheStatus.FORFEITED, platform.tranches["G1:4"].status)

    def test_new_tranche_added_by_amendment(self):
        platform = make_platform()
        new = specs() + [TrancheSpec(5, 1825, D("50"))]
        platform.amend_grant("G1", effective_from=date(2025, 1, 10), at=dt(2025, 1, 10, 2, 0), tranches=new)
        self.assertEqual(TrancheStatus.PENDING, platform.tranches["G1:5"].status)


class AmendmentProtectionTest(unittest.TestCase):
    def test_settled_tranche_cannot_be_modified(self):
        platform, as_of = vested_platform()
        platform.create_batch("B1", ["G1:1"], at=as_of)
        platform.submit_batch("B1")
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            platform.review_batch("B1", role=role, decision="APPROVED", at=as_of)
        platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        before = platform.settlements["G1:1"]
        with self.assertRaises(AmendmentError):
            platform.amend_grant(
                "G1",
                effective_from=date(2025, 1, 15),
                at=dt(2025, 1, 15, 2, 0),
                tranches=specs(("80", "100", "100", "100")),
            )
        # 已结算结果保持封存
        self.assertIs(before, platform.settlements["G1:1"])
        self.assertEqual(65, before.quote.delivered_shares)

    def test_vested_tranche_rights_cannot_be_reduced(self):
        platform, as_of = vested_platform()
        with self.assertRaises(AmendmentError):
            platform.amend_grant(
                "G1",
                effective_from=date(2025, 1, 15),
                at=dt(2025, 1, 15, 2, 0),
                tranches=specs(("90", "100", "100", "100")),
            )
        with self.assertRaises(AmendmentError):
            platform.amend_grant(
                "G1",
                effective_from=date(2025, 1, 15),
                at=dt(2025, 1, 15, 2, 0),
                tranches=specs()[1:],  # 删除已归属的第 1 期
            )

    def test_vested_tranche_may_be_increased(self):
        platform, as_of = vested_platform()
        platform.amend_grant(
            "G1",
            effective_from=date(2025, 1, 15),
            at=dt(2025, 1, 15, 2, 0),
            tranches=specs(("110", "100", "100", "100")),
        )
        self.assertEqual(2, platform.grants["G1"].terms_history[-1].version)


if __name__ == "__main__":
    unittest.main()
