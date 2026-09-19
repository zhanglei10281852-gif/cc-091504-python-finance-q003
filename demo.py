"""端到端演示：跨境员工股权激励归属与离职结算。

场景：员工 E1001 为中国税务居民，持有美元股票 ACME 的分期授予；
停薪休假暂停计时、绩效部分达标、协议向后修订、黑窗期锁定、
三方复核退回修正、并发结算防重、离职后窗口期处理。

运行：python3 demo.py
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from app import to_jsonable  # noqa: E402
from equity_platform import (  # noqa: E402
    EquityPlatform,
    FieldDiff,
    PerformanceCondition,
    SettlementMethod,
    TerminationType,
    TrancheSpec,
)
from equity_platform.errors import AmendmentError, WorkflowError  # noqa: E402
from equity_platform.reports import (  # noqa: E402
    employee_statement,
    failed_receipts,
    pending_approvals,
    post_termination_tranches,
)

D = Decimal
TZ = "Asia/Shanghai"


def dt(y, m, d, hh=0, mi=0):
    return datetime(y, m, d, hh, mi, tzinfo=timezone.utc)


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def main() -> None:
    platform = EquityPlatform()
    platform.register_employee("E1001", "跨境员工")

    # 1) 授予：400 股分 4 期，净股交付，第 1 期带绩效条件
    platform.create_grant(
        "G100",
        "E1001",
        "ACME",
        grant_date=date(2024, 1, 15),
        cutoff_timezone=TZ,
        tranches=[
            TrancheSpec(1, 365, D("100"), PerformanceCondition("revenue", D("1000"))),
            TrancheSpec(2, 730, D("100")),
            TrancheSpec(3, 1095, D("100")),
            TrancheSpec(4, 1460, D("100")),
        ],
        settlement_method=SettlementMethod.NET_SHARE,
    )
    platform.set_tax_identity("E1001", "CN", D("0.35"), "CNY")

    # 2) 停薪休假 30 天：归属计时暂停，归属日顺延
    platform.add_leave("E1001", "L1", date(2024, 3, 1), date(2024, 3, 31))

    # 3) 协议修订：只向后形成 v2，调整未归属期数
    platform.amend_grant(
        "G100",
        effective_from=date(2025, 2, 1),
        at=dt(2025, 1, 10, 2),
        tranches=[
            TrancheSpec(1, 365, D("100"), PerformanceCondition("revenue", D("1000"))),
            TrancheSpec(2, 730, D("100")),
            TrancheSpec(3, 1095, D("120")),
            TrancheSpec(4, 1460, D("120")),
        ],
    )

    # 4) 绩效评定 85%：部分达标
    platform.record_performance_actual("G100", 1, D("850"))

    # 5) 归属日（含休假顺延）重算
    platform.recalculate("G100", dt(2025, 2, 13, 2))
    t1 = platform.tranches["G100:1"]
    show("第 1 期归属（休假顺延 30 天，绩效 85%）", {
        "vest_date": t1.vest_date, "status": t1.status,
        "vested": t1.vested_shares, "forfeited": t1.forfeited_shares,
    })

    # 6) 自愿离职：未归属批次失效，已归属批次进入 90 天结算窗口
    platform.terminate("E1001", TerminationType.VOLUNTARY, date(2025, 2, 20), at=dt(2025, 2, 20, 2))
    show("离职后管理员视图", post_termination_tranches(platform, dt(2025, 2, 21, 2)))

    # 7) 黑窗期：批次锁定，执行被确定性拒绝
    platform.add_blackout("BW1", date(2025, 2, 25), date(2025, 3, 5), "年报静默期")
    platform.recalculate("G100", dt(2025, 2, 26, 2))
    platform.create_batch("SB1", ["G100:1"], at=dt(2025, 2, 26, 2))
    platform.submit_batch("SB1")
    for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
        platform.review_batch("SB1", role=role, decision="APPROVED", at=dt(2025, 2, 26, 3))
    try:
        platform.execute_batch("SB1", idempotency_key="sb1", as_of=dt(2025, 2, 26, 4))
    except WorkflowError as exc:
        show("黑窗期执行被拒绝", {"error": str(exc)})

    # 8) 黑窗结束，重新建批；薪资岗退回（预扣率差异），修正后重新提交
    platform.recalculate("G100", dt(2025, 3, 5, 2))
    platform.add_market_price("ACME", date(2025, 3, 5), "USD", D("52"))
    platform.add_fx_rate("USD", "CNY", date(2025, 3, 5), D("7.1"))
    platform.create_batch("SB2", ["G100:1"], at=dt(2025, 3, 5, 2))
    platform.submit_batch("SB2")
    platform.review_batch("SB2", role="HR", decision="APPROVED", at=dt(2025, 3, 5, 3))
    platform.review_batch(
        "SB2", role="PAYROLL", decision="REJECTED",
        comment="税务身份已更新，预扣率应为 45%",
        differences=[FieldDiff("withholding_rate", "0.35", "0.45")],
        at=dt(2025, 3, 5, 4),
    )
    platform.set_tax_identity("E1001", "CN", D("0.45"), "CNY")
    platform.resubmit_batch("SB2")
    for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
        platform.review_batch("SB2", role=role, decision="APPROVED", at=dt(2025, 3, 5, 5))
    show("退回意见与差异保留", [
        {"version": r.batch_version, "role": r.role, "decision": r.decision,
         "comment": r.comment, "differences": r.differences}
        for r in platform.batches["SB2"].reviews
    ])

    # 9) 并发结算：8 个线程同一幂等键，只扣股/付款一次
    outcomes = []

    def worker():
        outcomes.append(platform.execute_batch("SB2", idempotency_key="sb2", as_of=dt(2025, 3, 5, 6)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    show("并发结算后的台账余额", {
        "grant_pool:G100": platform.ledger.balance("grant_pool:G100", "SHARE"),
        "employee:E1001:shares": platform.ledger.balance("employee:E1001:shares", "SHARE"),
        "employee:E1001:cash": platform.ledger.balance("employee:E1001:cash", "USD"),
        "tax_authority:CN(CNY)": platform.ledger.balance("tax_authority:CN", "CNY"),
    })

    # 10) 券商回执失败 → 管理员追踪 → 重发成功
    platform.record_receipt("G100:1", "broker", False, "券商拒绝：账户冻结", at=dt(2025, 3, 5, 7))
    show("失败回执", failed_receipts(platform))
    platform.record_receipt("G100:1", "broker", True, "已交割", at=dt(2025, 3, 5, 8))

    # 11) 已结算批次不得倒改
    try:
        platform.amend_grant(
            "G100",
            effective_from=date(2025, 3, 6),
            at=dt(2025, 3, 6, 2),
            tranches=[TrancheSpec(1, 365, D("80")), TrancheSpec(2, 730, D("100"))],
        )
    except AmendmentError as exc:
        show("倒改已结算批次被拒绝", {"error": str(exc)})

    # 12) 员工对账单：条件、税费、汇率与到账构成
    statement = employee_statement(platform, "E1001", dt(2025, 3, 6, 2))
    show("员工对账单（第 1 期）", statement["tranches"][0])
    show("管理员待办", {
        "pending_approvals": pending_approvals(platform),
        "failed_receipts": failed_receipts(platform),
        "post_termination": post_termination_tranches(platform, dt(2025, 3, 6, 2)),
    })


if __name__ == "__main__":
    main()
