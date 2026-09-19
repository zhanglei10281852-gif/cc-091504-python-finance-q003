"""测试共享工厂：标准授予场景与常用时刻。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from equity_platform import (  # noqa: E402
    EquityPlatform,
    PerformanceCondition,
    SettlementMethod,
    TrancheSpec,
)

TZ = "Asia/Shanghai"
D = Decimal
GRANT_DATE = date(2024, 1, 15)
VEST_1 = date(2025, 1, 14)  # 2024 为闰年：2024-01-15 + 365 天


def dt(y, m, d, hh=0, mi=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mi, ss, tzinfo=timezone.utc)


def make_platform(*, method=SettlementMethod.NET_SHARE, performance: bool = False) -> EquityPlatform:
    """4 期各 100 股，授予日 2024-01-15，协议时区 Asia/Shanghai。"""
    platform = EquityPlatform()
    platform.register_employee("E1", "跨境员工")
    specs = [
        TrancheSpec(
            sequence=seq,
            required_service_days=365 * seq,
            shares=D("100"),
            performance=PerformanceCondition("revenue", D("1000")) if performance else None,
        )
        for seq in (1, 2, 3, 4)
    ]
    platform.create_grant(
        "G1",
        "E1",
        "ACME",
        grant_date=GRANT_DATE,
        cutoff_timezone=TZ,
        tranches=specs,
        settlement_method=method,
    )
    return platform


def vested_platform(*, method=SettlementMethod.NET_SHARE, performance: bool = False):
    """第一期已归属（可执行），价格与税务身份就绪。"""
    platform = make_platform(method=method, performance=performance)
    if performance:
        platform.record_performance_actual("G1", 1, D("850"))
    as_of = dt(2025, 1, 14, 2, 0)  # 上海时间 2025-01-14 10:00
    platform.recalculate("G1", as_of)
    platform.set_tax_identity("E1", "CN", D("0.35"), "USD")
    platform.add_market_price("ACME", VEST_1, "USD", D("50"))
    return platform, as_of
