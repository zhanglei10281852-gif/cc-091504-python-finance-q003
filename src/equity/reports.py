"""报表:员工对账单与管理员追踪视图。

员工对账单:每次归属的条件(基准/调整归属日、休假天数、绩效达成与比例)、
税费、汇率、到账构成,全部来自结算时锁定的快照,可逐项核对。

管理员视图:尚未完成的审批、失败回执、离职后仍需处理的批次。
"""

from __future__ import annotations

from .enums import ReceiptStatus, ReviewDecision, Role, TrancheState
from .service import EquityService

TERMINAL = (TrancheState.SETTLED, TrancheState.LAPSED)


def _breakdown_dict(b) -> dict:
    return {
        "结算方式": b.mode.value,
        "应归属股数": str(b.gross_shares),
        "整股数": str(b.whole_shares),
        "零股": str(b.fractional_shares),
        "市价": {"price": str(b.price), "currency": b.price_currency, "at": b.price_at.isoformat()},
        "汇率": (
            {"rate": str(b.fx_rate), "at": b.fx_at.isoformat()}
            if b.fx_rate is not None
            else None
        ),
        "税务币种": b.tax_currency,
        "应纳税所得额": str(b.taxable_income_tax_ccy),
        "预扣税": str(b.tax_due_tax_ccy),
        "扣股抵税": str(b.shares_for_tax),
        "到账股数": str(b.net_shares),
        "零股现金补偿": str(b.fractional_cash_tax_ccy),
        "卖股剩余返还": str(b.sell_residual_tax_ccy),
        "多扣退还": str(b.withhold_refund_tax_ccy),
        "员工应补现金": str(b.employee_cash_due_tax_ccy),
        "工资代扣税额": str(b.payroll_withholding_tax_ccy),
    }


def employee_statement(service: EquityService, employee_id: str) -> dict:
    grants = [
        g for g in service.store.state.grants.values() if g.employee_id == employee_id
    ]
    statement = {"employee_id": employee_id, "grants": []}
    for grant in grants:
        leave_days = sum(l.days for l in grant.leaves)
        grant_view = {
            "grant_id": grant.grant_id,
            "award_type": grant.award_type,
            "stock_currency": grant.stock_currency,
            "service_start": str(grant.service_start),
            "协议版本数": len(grant.versions),
            "离职": (
                {"type": grant.termination.type.value, "date": str(grant.termination.date)}
                if grant.termination
                else None
            ),
            "tranches": [],
        }
        for record in sorted(grant.tranches.values(), key=lambda r: r.terms.sequence):
            case = service.store.state.approval_cases.get(record.terms.tranche_id)
            approvals = {}
            if case:
                for role, rec in case.latest_by_role().items():
                    approvals[role.value] = rec.decision.value
            view = {
                "tranche_id": record.terms.tranche_id,
                "状态": record.state.value,
                "条件": {
                    "基准归属日": str(record.terms.base_vest_date),
                    "调整后归属日": (
                        str(record.adjusted_vest_date) if record.adjusted_vest_date else None
                    ),
                    "累计休假天数": leave_days,
                    "绩效指标": record.terms.performance_metric,
                    "达成率": str(record.achievement) if record.achievement else None,
                    "归属比例": str(record.payout) if record.payout else None,
                },
                "应归属股数": (
                    str(record.eligible_shares) if record.eligible_shares is not None else None
                ),
                "管辖协议版本": record.governing_version,
                "行权截止": (
                    record.exercise_deadline.isoformat() if record.exercise_deadline else None
                ),
                "失效原因": record.lapse_reason,
                "审批": approvals,
                "结算": None,
            }
            if record.receipt_id:
                receipt = service.store.state.receipts[record.receipt_id]
                view["结算"] = {
                    "receipt_id": receipt.receipt_id,
                    "状态": receipt.status.value,
                    "确认号": receipt.confirmation_no,
                    "构成": (
                        _breakdown_dict(receipt.breakdown) if receipt.breakdown else None
                    ),
                }
            grant_view["tranches"].append(view)
        statement["grants"].append(grant_view)
    return statement


def admin_dashboard(service: EquityService) -> dict:
    state = service.store.state

    pending_approvals = []
    for case in state.approval_cases.values():
        grant, record = service.find_tranche(case.tranche_id)
        expected = service._expected_hashes(grant, record)
        latest = case.latest_by_role()
        missing = []
        for role in Role:
            rec = latest.get(role)
            if (
                rec is None
                or rec.decision != ReviewDecision.CONFIRMED
                or rec.snapshot_hash != expected[role]
            ):
                missing.append(role.value)
        if missing and record.state not in TERMINAL:
            pending_approvals.append(
                {
                    "case_id": case.case_id,
                    "grant_id": case.grant_id,
                    "tranche_id": case.tranche_id,
                    "待确认岗位": missing,
                    "退回记录数": sum(
                        1
                        for r in case.reviews
                        if r.decision == ReviewDecision.RETURNED
                    ),
                }
            )

    failed_receipts = [
        {
            "receipt_id": r.receipt_id,
            "grant_id": r.grant_id,
            "tranche_id": r.tranche_id,
            "employee_id": r.employee_id,
            "失败原因": r.failure_reason,
            "attempt": r.attempt,
            "at": r.created_at.isoformat(),
        }
        for r in state.receipts.values()
        if r.status == ReceiptStatus.FAILED
    ]

    post_termination_open = []
    for grant in state.grants.values():
        if grant.termination is None:
            continue
        open_tranches = [
            {
                "tranche_id": r.terms.tranche_id,
                "状态": r.state.value,
                "行权截止": (
                    r.exercise_deadline.isoformat() if r.exercise_deadline else None
                ),
            }
            for r in grant.tranches.values()
            if r.state not in TERMINAL
        ]
        if open_tranches:
            post_termination_open.append(
                {
                    "grant_id": grant.grant_id,
                    "employee_id": grant.employee_id,
                    "离职类型": grant.termination.type.value,
                    "离职日": str(grant.termination.date),
                    "待处理批次": open_tranches,
                }
            )

    return {
        "待完成审批": pending_approvals,
        "失败回执": failed_receipts,
        "离职后待处理": post_termination_open,
    }
