from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import GRANT, PERF_CURVE, at, make_grant, make_service, tranche

from equity.enums import SettlementMode, TrancheState
from equity.errors import ConflictError, ValidationError
from equity.models import TrancheTerms


def v2_tranches(t1_shares="1000", t2_shares="1000"):
    return [
        TrancheTerms("T1", 1, Decimal(t1_shares), date(2024, 1, 1)),
        TrancheTerms("T2", 2, Decimal(t2_shares), date(2025, 1, 1), "REV2024", PERF_CURVE),
        TrancheTerms("T3", 3, Decimal("1000"), date(2026, 1, 1)),
        TrancheTerms("T4", 4, Decimal("1000"), date(2027, 1, 1)),
    ]


class AmendmentTest(unittest.TestCase):
    def test_amendment_appends_version_forward_only(self):
        service = make_service()
        make_grant(service)
        version = service.amend_grant(
            GRANT,
            effective_from=date(2024, 6, 1),
            settlement_mode=SettlementMode.SELL_TO_COVER,
            tranches=v2_tranches(),
            note="调整结算方式",
            now=at(2024, 5, 1),
        )
        self.assertEqual(2, version.version)
        self.assertEqual(2, len(service.get_grant(GRANT).versions))

    def test_retroactive_amendment_rejected(self):
        service = make_service()
        make_grant(service)
        with self.assertRaises(ValidationError) as ctx:
            service.amend_grant(
                GRANT,
                effective_from=date(2024, 1, 1),
                settlement_mode=SettlementMode.GROSS,
                tranches=v2_tranches(),
                note="追溯修改",
                now=at(2024, 5, 1),
            )
        self.assertEqual("retroactive_amendment", ctx.exception.code)

    def test_settled_tranche_immutable(self):
        service = make_service()
        make_grant(service)
        service.evaluate_grant(GRANT, now=at(2024, 1, 2))
        helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
        service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))
        # 试图改变已结算批次股数 -> 拒绝
        with self.assertRaises(ConflictError) as ctx:
            service.amend_grant(
                GRANT,
                effective_from=date(2024, 2, 1),
                settlement_mode=SettlementMode.NET_SHARE,
                tranches=v2_tranches(t1_shares="800"),
                note="倒改已结算批次",
                now=at(2024, 2, 1),
            )
        self.assertEqual("settled_tranche_immutable", ctx.exception.code)
        # 已结算批次条款不变、只改未归属批次 -> 允许
        version = service.amend_grant(
            GRANT,
            effective_from=date(2024, 2, 1),
            settlement_mode=SettlementMode.SELL_TO_COVER,
            tranches=v2_tranches(t2_shares="1200"),
            note="T2 调整为 1200 股",
            now=at(2024, 2, 1),
        )
        self.assertEqual(2, version.version)
        self.assertEqual(TrancheState.SETTLED, tranche(service, "T1").state)

    def test_missing_tranche_rejected(self):
        service = make_service()
        make_grant(service)
        with self.assertRaises(ValidationError) as ctx:
            service.amend_grant(
                GRANT,
                effective_from=date(2024, 6, 1),
                settlement_mode=SettlementMode.GROSS,
                tranches=v2_tranches()[:2],
                note="缺漏批次",
                now=at(2024, 5, 1),
            )
        self.assertEqual("tranche_missing", ctx.exception.code)

    def test_governing_version_locked_at_vesting(self):
        service = make_service()
        make_grant(service)
        # T2 在 v1 下归属(绩效 100 -> 全额 1000 股)
        helpers.assess(service, "100")
        service.evaluate_grant(GRANT, now=at(2025, 1, 2))
        record = tranche(service, "T2")
        self.assertEqual(1, record.governing_version)
        self.assertEqual(Decimal("1000.0000"), record.eligible_shares)
        # v2 把 T2 改为 1200 股,已归属批次不受影响
        service.amend_grant(
            GRANT,
            effective_from=date(2025, 2, 1),
            settlement_mode=SettlementMode.NET_SHARE,
            tranches=v2_tranches(t2_shares="1200"),
            note="T2 调股",
            now=at(2025, 2, 1),
        )
        record = tranche(service, "T2")
        self.assertEqual(1, record.governing_version)
        self.assertEqual(Decimal("1000.0000"), record.eligible_shares)

    def test_future_tranche_uses_new_version_terms(self):
        service = make_service()
        make_grant(service)
        service.amend_grant(
            GRANT,
            effective_from=date(2025, 6, 1),
            settlement_mode=SettlementMode.NET_SHARE,
            tranches=[
                TrancheTerms("T1", 1, Decimal("1000"), date(2024, 1, 1)),
                TrancheTerms("T2", 2, Decimal("1000"), date(2025, 1, 1), "REV2024", PERF_CURVE),
                TrancheTerms("T3", 3, Decimal("1500"), date(2026, 1, 1)),
                TrancheTerms("T4", 4, Decimal("1000"), date(2027, 1, 1)),
            ],
            note="T3 调整为 1500 股",
            now=at(2025, 6, 1),
        )
        service.evaluate_grant(GRANT, now=at(2026, 1, 2))
        record = tranche(service, "T3")
        self.assertEqual(Decimal("1500.0000"), record.eligible_shares)
        self.assertEqual(2, record.governing_version)


if __name__ == "__main__":
    unittest.main()
