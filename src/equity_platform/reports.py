"""员工对账与管理员追踪视图：同一套口径，两个视角。"""
from __future__ import annotations

from datetime import datetime

from .approvals import BatchStatus
from .models import Tranche, TrancheStatus
from .platform import EquityPlatform
from .timeutil import local_date
from .vesting import leave_days_between, service_days_between


def employee_statement(platform: EquityPlatform, employee_id: str, as_of: datetime) -> dict:
    """员工对账单：每次归属的条件、税费、汇率与到账构成。"""
    grants = [g for g in platform.grants.values() if g.employee_id == employee_id]
    leaves = platform.leaves.get(employee_id, [])
    termination = platform.terminations.get(employee_id)
    items = []
    for grant in grants:
        terms = grant.terms_history[-1]
        day = local_date(terms.cutoff_timezone, as_of)
        end_day = day
        if termination is not None and termination.termination_date < day:
            end_day = termination.termination_date
        tranches = sorted(
            (t for t in platform.tranches.values() if t.grant_id == grant.grant_id),
            key=lambda t: t.sequence,
        )
        for tranche in tranches:
            items.append(_tranche_statement(platform, tranche, leaves, terms.grant_date, end_day))
    return {
        "employee_id": employee_id,
        "generated_at": as_of.isoformat(),
        "tranches": items,
    }


def _tranche_statement(
    platform: EquityPlatform,
    tranche: Tranche,
    leaves,
    grant_date,
    end_day,
) -> dict:
    actual = platform.performance_actuals.get((tranche.grant_id, tranche.sequence))
    performance = None
    if tranche.performance is not None:
        performance = {
            "metric": tranche.performance.metric,
            "target": tranche.performance.target,
            "actual": actual,
            "factor": tranche.performance_factor,
        }
    statement = {
        "tranche_id": tranche.tranche_id,
        "sequence": tranche.sequence,
        "status": tranche.status.value,
        "terms_version": tranche.terms_version,
        "service": {
            "required_service_days": tranche.required_service_days,
            "credited_service_days": service_days_between(grant_date, end_day, leaves),
            "leave_days_deducted": leave_days_between(grant_date, end_day, leaves),
            "vest_date": tranche.vest_date,
        },
        "performance": performance,
        "shares": {
            "granted": tranche.spec_shares,
            "vested": tranche.vested_shares,
            "forfeited": tranche.forfeited_shares,
        },
        "settlement_deadline": tranche.settlement_deadline,
        "settlement": _settlement_statement(tranche),
        "history": [
            {
                "at": e.at.isoformat(),
                "from": e.from_status.value if e.from_status else None,
                "to": e.to_status.value,
                "reason": e.reason,
            }
            for e in tranche.events
        ],
    }
    return statement


def _settlement_statement(tranche: Tranche) -> dict | None:
    record = tranche.settlement
    if record is None:
        return None
    quote = record.quote
    return {
        "settlement_id": record.settlement_id,
        "terms_version": record.terms_version,
        "method": record.method.value,
        "price": quote.price,
        "price_currency": quote.price_currency,
        "price_date": record.price_date,
        "fx_rate": quote.fx_rate,
        "tax_currency": quote.tax_currency,
        "withholding_rate": quote.withholding_rate,
        "gross_value": quote.gross_value,
        "taxable_income_tax_ccy": quote.taxable_income_tax_ccy,
        "tax_due_tax_ccy": quote.tax_due_tax_ccy,
        "tax_due_price_ccy": quote.tax_due_price_ccy,
        "shares_for_tax": quote.shares_for_tax,
        "tax_cover_value": quote.tax_cover_value,
        "cash_residual_to_employee": quote.cash_residual_to_employee,
        "cash_tax_due_from_employee": quote.cash_tax_due_from_employee,
        "delivered_shares": quote.delivered_shares,
        "fractional_shares": quote.fractional_shares,
        "cash_in_lieu": quote.cash_in_lieu,
        "total_cash_to_employee": quote.total_cash_to_employee,
        "settled_at": record.settled_at.isoformat(),
        "idempotency_key": record.idempotency_key,
        "ledger_entry_ids": list(record.ledger_entry_ids),
    }


def pending_approvals(platform: EquityPlatform) -> list[dict]:
    """管理员视图：尚未完成三方确认的结算批次。"""
    return [
        {
            "batch_id": batch.batch_id,
            "version": batch.version,
            "missing_roles": sorted(role.value for role in batch.missing_roles()),
            "tranche_ids": list(batch.tranche_ids),
        }
        for batch in platform.batches.values()
        if batch.status is BatchStatus.IN_REVIEW
    ]


def failed_receipts(platform: EquityPlatform) -> list[dict]:
    """管理员视图：最新回执仍为失败的（批次, 渠道）组合。"""
    latest: dict[tuple[str, str], object] = {}
    for receipt in platform.receipts:
        latest[(receipt.tranche_id, receipt.channel)] = receipt
    return [
        {
            "tranche_id": receipt.tranche_id,
            "channel": receipt.channel,
            "detail": receipt.detail,
            "received_at": receipt.received_at.isoformat(),
        }
        for receipt in latest.values()
        if not receipt.ok
    ]


def post_termination_tranches(platform: EquityPlatform, as_of: datetime) -> list[dict]:
    """管理员视图：员工已离职但仍有待处理（锁定/可执行）的归属批次。"""
    items = []
    for employee_id, termination in platform.terminations.items():
        for tranche in platform.tranches.values():
            if tranche.employee_id != employee_id:
                continue
            if tranche.status not in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE):
                continue
            days_remaining = None
            if tranche.settlement_deadline is not None:
                today = local_date(tranche.cutoff_timezone, as_of)
                days_remaining = (tranche.settlement_deadline - today).days
            items.append(
                {
                    "employee_id": employee_id,
                    "tranche_id": tranche.tranche_id,
                    "status": tranche.status.value,
                    "termination_type": termination.termination_type.value,
                    "termination_date": termination.termination_date,
                    "settlement_deadline": tranche.settlement_deadline,
                    "days_remaining": days_remaining,
                }
            )
    return sorted(items, key=lambda item: item["tranche_id"])
