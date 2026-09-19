"""归属引擎：有效服务天数累计（停薪休假暂停计时）与归属日推算。"""
from __future__ import annotations

from datetime import date, timedelta

from .models import LeavePeriod


def leave_days_between(start: date, end: date, leaves: list[LeavePeriod]) -> int:
    """[start, end) 内暂停计时的休假天数（多段休假按区间重叠去重前分别累计）。"""
    from .models import DateRange

    span = DateRange(start, end)
    return sum(leave.period.overlap_days(span) for leave in leaves if leave.suspends_vesting)


def service_days_between(start: date, end: date, leaves: list[LeavePeriod]) -> int:
    """有效服务天数 = 日历天数 - 暂停计时的休假天数。"""
    if end <= start:
        return 0
    return (end - start).days - leave_days_between(start, end, leaves)


def compute_vest_date(
    grant_date: date, required_service_days: int, leaves: list[LeavePeriod]
) -> date:
    """归属日 = 有效服务天数首次达到要求之日。

    休假期间不计入服务天数，归属日相应顺延；顺延后再落入新的休假段则继续顺延，
    迭代至稳定（休假区间有限，必然收敛）。
    """
    candidate = grant_date + timedelta(days=required_service_days)
    while True:
        paused = leave_days_between(grant_date, candidate, leaves)
        shifted = grant_date + timedelta(days=required_service_days + paused)
        if shifted == candidate:
            return candidate
        candidate = shifted
