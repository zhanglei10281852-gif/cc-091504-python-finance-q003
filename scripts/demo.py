#!/usr/bin/env python3
"""端到端演示:跨境员工离职结算场景。

员工 E1001(中国税务居民)持有美元 RSU,四期归属。演示:
停薪休假暂停计时、绩效部分达标、协议版本、三岗位审批(含一次退回留痕)、
黑窗期顺延、净股交付、零股现金补偿、离职后批次处理、失败回执重试、
并发结算防重、员工对账与管理员追踪。

运行:python3 scripts/demo.py
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from equity.codec import plain
from equity.enums import ReviewDecision, Role, SettlementMode, TerminationType
from equity.gateway import DepositoryGateway
from equity.models import (
    BlackoutWindow,
    FxRatePoint,
    LeavePeriod,
    PerformanceAssessment,
    PayoutStep,
    PricePoint,
    TaxBracket,
    TaxProfile,
    Termination,
    TrancheTerms,
)
from equity.reports import admin_dashboard, employee_statement
from equity.service import EquityService

UTC = timezone.utc


def at(y, m, d, hh=0):
    return datetime(y, m, d, hh, tzinfo=UTC)


def show(title, payload):
    print(f"\n===== {title} =====")
    print(json.dumps(plain(payload), ensure_ascii=False, indent=2, default=str))


def note(text):
    print(f"\n--- {text} ---")


def main() -> None:
    gateway = DepositoryGateway()
    service = EquityService(gateway=gateway)

    note("1. 市场数据:美元市价与 USD->CNY 汇率(as-of 快照)")
    service.add_price(PricePoint(at=at(2023, 1, 1), currency="USD", price=Decimal("50")))
    service.add_price(PricePoint(at=at(2025, 1, 1), currency="USD", price=Decimal("62.5")))
    service.add_fx(FxRatePoint(at=at(2023, 1, 1), base="USD", quote="CNY", rate=Decimal("7.0")))
    service.add_fx(FxRatePoint(at=at(2025, 1, 1), base="USD", quote="CNY", rate=Decimal("7.2")))

    note("2. 授予 GR-1:四期 RSU,T2 附绩效阶梯(达成 80% 归属 50%,100% 全额)")
    service.create_grant(
        grant_id="GR-1",
        employee_id="E1001",
        award_type="RSU",
        stock_currency="USD",
        service_start=date(2023, 1, 1),
        effective_from=date(2023, 1, 1),
        settlement_mode=SettlementMode.NET_SHARE,
        tranches=[
            TrancheTerms("T1", 1, Decimal("1000"), date(2024, 1, 1)),
            TrancheTerms(
                "T2", 2, Decimal("1000.5"), date(2025, 1, 1), "REV2024",
                (
                    PayoutStep(Decimal("0.8"), Decimal("0.5")),
                    PayoutStep(Decimal("1.0"), Decimal("1.0")),
                ),
            ),
            TrancheTerms("T3", 3, Decimal("1000"), date(2026, 1, 1)),
            TrancheTerms("T4", 4, Decimal("1000"), date(2027, 1, 1)),
        ],
        now=at(2023, 1, 1),
    )

    note("3. T1 归属;薪资岗退回(缺少税务身份资料),补录后重新确认,差异留痕")
    service.evaluate_grant("GR-1", now=at(2024, 1, 2))
    service.review("T1", role=Role.HR, decision=ReviewDecision.CONFIRMED, opinion="服务期间核对无误", now=at(2024, 1, 3))
    service.review("T1", role=Role.PAYROLL, decision=ReviewDecision.RETURNED, opinion="缺少该员工税务身份资料,无法核对预扣口径", now=at(2024, 1, 3))
    service.set_tax_profile(
        TaxProfile(
            employee_id="E1001",
            jurisdiction="CN",
            residency="RESIDENT",
            tax_currency="CNY",
            brackets=(
                TaxBracket(Decimal("0"), Decimal("0.03")),
                TaxBracket(Decimal("36000"), Decimal("0.10")),
                TaxBracket(Decimal("144000"), Decimal("0.20")),
                TaxBracket(Decimal("300000"), Decimal("0.25")),
            ),
        )
    )
    reconfirmed = service.review("T1", role=Role.PAYROLL, decision=ReviewDecision.CONFIRMED, opinion="资料已补录,预扣口径确认", now=at(2024, 1, 4))
    service.review("T1", role=Role.SECURITIES, decision=ReviewDecision.CONFIRMED, opinion="批次数量与协议一致", now=at(2024, 1, 4))
    show("薪资岗退回后重新确认所附差异", reconfirmed.diff_from_previous)

    note("4. T1 净股结算:税 55580 CNY,扣 159 股,多扣 70 CNY 现金退还")
    receipt_t1 = service.settle("T1", idempotency_key="SETTLE-T1", now=at(2024, 1, 5))
    show("T1 结算构成", receipt_t1.breakdown)

    note("5. 绩效评定:REV2024 达成 90% -> 阶梯归属 50%")
    service.assess_performance(
        "GR-1",
        PerformanceAssessment("REV2024", Decimal("100"), Decimal("90"), at(2024, 3, 1)),
        now=at(2024, 3, 1),
    )

    note("6. 2024-06 停薪休假 30 天:T2 归属日 2025-01-01 顺延至 2025-01-31")
    service.record_leave("GR-1", LeavePeriod(date(2024, 6, 1), date(2024, 6, 30), "停薪留职"), now=at(2024, 6, 1))
    service.evaluate_grant("GR-1", now=at(2025, 1, 31, 16))
    record_t2 = service.find_tranche("T2")[1]
    print(f"T2 调整后归属日: {record_t2.adjusted_vest_date},应归属: {record_t2.eligible_shares} 股")

    note("7. 年报黑窗期(2025-01-20~02-10):审批齐全也不可执行,黑窗结束自动放行")
    service.add_blackout(BlackoutWindow(at(2025, 1, 20), at(2025, 2, 10), "年报静默期"))
    for role in (Role.HR, Role.PAYROLL, Role.SECURITIES):
        service.review("T2", role=role, decision=ReviewDecision.CONFIRMED, opinion="核对无误", now=at(2025, 2, 1))
    print(f"黑窗期内 T2 状态: {record_t2.state.value}")
    service.evaluate_grant("GR-1", now=at(2025, 2, 11))
    print(f"黑窗结束后 T2 状态: {record_t2.state.value}")

    note("8. 离职员工证券账户冻结:T2 首次结算失败,产生失败回执")
    gateway.block_account("E1001")
    failed = service.settle("T2", idempotency_key="SETTLE-T2", now=at(2025, 2, 12))
    print(f"结算结果: {failed.status.value} - {failed.failure_reason}(批次状态仍为 {record_t2.state.value},未扣股)")

    note("9. 2025-07-01 主动离职:T3/T4 未归属作废;离职事实使 T2 人事确认失效,需复核")
    service.record_termination("GR-1", Termination(TerminationType.VOLUNTARY, date(2025, 7, 1)), now=at(2025, 7, 1))
    grant = service.get_grant("GR-1")
    show("离职后各批次状态", {tid: r.state.value for tid, r in grant.tranches.items()})
    print(f"T2 行权截止(UTC): {record_t2.exercise_deadline}")
    show("管理员视图(离职后待处理 + 失败回执 + 待审批)", admin_dashboard(service))

    note("10. 人事复核离职事实后重新确认 T2,账户解冻,失败回执换新幂等键重试")
    service.review("T2", role=Role.HR, decision=ReviewDecision.CONFIRMED, opinion="离职类型与日期核对无误", now=at(2025, 7, 1, 2))
    gateway.unblock_account("E1001")
    receipt_t2 = service.retry_receipt(failed.receipt_id, new_idempotency_key="SETTLE-T2-R", now=at(2025, 7, 2))
    show("T2 结算构成(部分达标 50% + 零股现金补偿,2025 年价格与汇率快照)", receipt_t2.breakdown)

    note("11. 并发:16 个线程同一幂等键结算 T1,只执行一次")
    results = []

    def worker():
        results.append(service.settle("T1", idempotency_key="SETTLE-T1", now=at(2024, 1, 5)))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"16 次提交返回 {len({r.receipt_id for r in results})} 张回执,T1 累计扣股 {service.shares_debited('T1')} 股")

    note("12. 员工对账单:每次归属的条件、税费、汇率与到账构成")
    statement = employee_statement(service, "E1001")
    show("T2 批次对账", statement["grants"][0]["tranches"][1])


if __name__ == "__main__":
    main()
