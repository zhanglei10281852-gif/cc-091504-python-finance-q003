"""测试共享场景:跨境员工 E1001,美元股票、中国税务居民、四期归属。"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from equity.enums import ReviewDecision, Role, SettlementMode  # noqa: E402
from equity.models import (  # noqa: E402
    FxRatePoint,
    PayoutStep,
    PricePoint,
    TaxBracket,
    TaxProfile,
    TrancheTerms,
)
from equity.service import EquityService  # noqa: E402

UTC = timezone.utc

EMPLOYEE = "E1001"
GRANT = "GR-1"

CNY_BRACKETS = (
    TaxBracket(lower=Decimal("0"), rate=Decimal("0.03")),
    TaxBracket(lower=Decimal("36000"), rate=Decimal("0.10")),
    TaxBracket(lower=Decimal("144000"), rate=Decimal("0.20")),
    TaxBracket(lower=Decimal("300000"), rate=Decimal("0.25")),
)

PERF_CURVE = (
    PayoutStep(threshold=Decimal("0.8"), ratio=Decimal("0.5")),
    PayoutStep(threshold=Decimal("1.0"), ratio=Decimal("1.0")),
)


def at(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def make_service(**kwargs) -> EquityService:
    service = EquityService(**kwargs)
    service.set_tax_profile(
        TaxProfile(
            employee_id=EMPLOYEE,
            jurisdiction="CN",
            residency="RESIDENT",
            tax_currency="CNY",
            brackets=CNY_BRACKETS,
        )
    )
    service.add_price(PricePoint(at=at(2023, 1, 1), currency="USD", price=Decimal("50")))
    service.add_fx(FxRatePoint(at=at(2023, 1, 1), base="USD", quote="CNY", rate=Decimal("7.0")))
    return service


def make_grant(service: EquityService, mode=SettlementMode.NET_SHARE) -> None:
    service.create_grant(
        grant_id=GRANT,
        employee_id=EMPLOYEE,
        award_type="RSU",
        stock_currency="USD",
        service_start=date(2023, 1, 1),
        effective_from=date(2023, 1, 1),
        settlement_mode=mode,
        tranches=[
            TrancheTerms("T1", 1, Decimal("1000"), date(2024, 1, 1)),
            TrancheTerms("T2", 2, Decimal("1000"), date(2025, 1, 1), "REV2024", PERF_CURVE),
            TrancheTerms("T3", 3, Decimal("1000"), date(2026, 1, 1)),
            TrancheTerms("T4", 4, Decimal("1000"), date(2027, 1, 1)),
        ],
        now=at(2023, 1, 1),
    )


def assess(service, actual: str, target: str = "100", when=None) -> None:
    from equity.models import PerformanceAssessment

    service.assess_performance(
        GRANT,
        PerformanceAssessment(
            metric="REV2024",
            target=Decimal(target),
            actual=Decimal(actual),
            assessed_at=when or at(2024, 3, 1),
        ),
        now=when or at(2024, 3, 1),
    )


def confirm_all(service, tranche_id: str, when=None) -> None:
    when = when or at(2024, 1, 2)
    for role in (Role.HR, Role.PAYROLL, Role.SECURITIES):
        service.review(
            tranche_id, role=role, decision=ReviewDecision.CONFIRMED, opinion="核对无误", now=when
        )


def tranche(service, tranche_id: str):
    return service.find_tranche(tranche_id)[1]
