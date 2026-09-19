"""归属引擎(纯函数,不触碰仓储)。

职责:
- 停薪休假暂停计时:实际归属日 = 基准归属日 + 此前累计休假天数(不动点收敛);
- 绩效部分达标:达成率按阶梯曲线映射归属比例,未达标(比例 0)批次失效;
- 离职政策:按离职类型决定未归属批次作废/加速、已归属未结算批次的行权窗口;
- 黑窗期:期内禁止执行,截止点落在黑窗内顺延到下一个开放时刻;
- 跨时区:截止点按授予时区日末换算 UTC 后比较。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN

from .constants import RATIO_QUANT, SHARE_QUANT
from .enums import TerminationType, TrancheState
from .models import (
    BlackoutWindow,
    Grant,
    LeavePeriod,
    PayoutStep,
    TrancheRecord,
    Transition,
)
from .timeutil import add_days, end_of_day_utc, local_date


@dataclass(frozen=True)
class TerminationRule:
    """离职政策规则。"""

    acceleration_ratio: Decimal      # 归属日晚于离职日的批次可加速归属的比例
    exercise_window_days: int        # 已归属未结算批次的行权窗口(天)
    forfeit_vested_unsettled: bool   # 已归属未结算是否一并作废(过失解除)


DEFAULT_TERMINATION_POLICY: dict[TerminationType, TerminationRule] = {
    TerminationType.VOLUNTARY: TerminationRule(Decimal("0"), 90, False),
    TerminationType.INVOLUNTARY: TerminationRule(Decimal("0.5"), 180, False),
    TerminationType.FOR_CAUSE: TerminationRule(Decimal("0"), 0, True),
    TerminationType.RETIREMENT: TerminationRule(Decimal("1"), 365, False),
    TerminationType.DEATH_DISABILITY: TerminationRule(Decimal("1"), 365, False),
}


def _effective_leave_days(leave: LeavePeriod, service_start: date, service_end: date | None) -> int:
    """休假计入归属暂停的天数:截断到服务期间内。"""
    start = max(leave.start, service_start)
    end = leave.end if service_end is None else min(leave.end, service_end)
    if start > end:
        return 0
    return (end - start).days + 1


def adjusted_vest_date(
    base: date,
    service_start: date,
    leaves: list[LeavePeriod],
    service_end: date | None = None,
) -> date:
    """实际归属日 = 基准归属日 + 落在归属日前的累计休假天数。

    休假天数随归属日后移可能覆盖更多休假区间,迭代至不动点;
    计入量单调不减且有上界(服务期内总休假天数),必然收敛。
    """
    adjusted = base
    while True:
        extra = sum(
            _effective_leave_days(leave, service_start, service_end)
            for leave in leaves
            if max(leave.start, service_start) <= adjusted
        )
        shifted = add_days(base, extra)
        if shifted == adjusted:
            return adjusted
        adjusted = shifted


def achievement_ratio(assessment_actual: Decimal, assessment_target: Decimal) -> Decimal:
    """达成率 = actual / target,4 位小数向下舍入(不利方向取保守值)。"""
    if assessment_target <= 0:
        return Decimal("0")
    return (assessment_actual / assessment_target).quantize(
        RATIO_QUANT, rounding=ROUND_DOWN
    )


def payout_ratio(curve: tuple[PayoutStep, ...], achievement: Decimal) -> Decimal:
    """阶梯曲线:取达成率满足的最高档;空曲线视为全额时间归属。"""
    if not curve:
        return Decimal("1")
    ratio = Decimal("0")
    for step in sorted(curve, key=lambda s: s.threshold):
        if achievement >= step.threshold:
            ratio = step.ratio
    return ratio


def quantize_shares(shares: Decimal) -> Decimal:
    """归属数量 4 位小数向下舍入,避免超发。"""
    return shares.quantize(SHARE_QUANT, rounding=ROUND_DOWN)


def in_blackout(at: datetime, blackouts: list[BlackoutWindow]) -> bool:
    return any(b.start <= at < b.end for b in blackouts)


def roll_out_of_blackout(at: datetime, blackouts: list[BlackoutWindow]) -> datetime:
    """时刻落在黑窗内则顺延到黑窗结束;可连续穿越多个黑窗,结果确定。"""
    moved = True
    while moved:
        moved = False
        for window in sorted(blackouts, key=lambda b: b.start):
            if window.start <= at < window.end:
                at = window.end
                moved = True
    return at


def exercise_deadline(
    grant: Grant,
    rule: TerminationRule,
    blackouts: list[BlackoutWindow],
) -> datetime | None:
    """行权截止:离职日 + 窗口天数,按授予时区日末换算 UTC,再顺延出黑窗。"""
    termination = grant.termination
    if termination is None:
        return None
    window_days = (
        termination.exercise_window_days
        if termination.exercise_window_days is not None
        else rule.exercise_window_days
    )
    deadline_local = add_days(termination.date, window_days)
    deadline_utc = end_of_day_utc(deadline_local, grant.timezone)
    return roll_out_of_blackout(deadline_utc, blackouts)


def evaluate_tranche(
    grant: Grant,
    record: TrancheRecord,
    now: datetime,
    blackouts: list[BlackoutWindow],
    approvals_complete: bool,
    policy: dict[TerminationType, TerminationRule] | None = None,
) -> Transition | None:
    """推进单个批次状态,返回一次状态转移;无需转移时返回 None。

    可重复调用(时间推进、审批变化、黑窗变化后重新评估),幂等。
    """
    policy = policy or DEFAULT_TERMINATION_POLICY
    state = record.state
    if state in (TrancheState.SETTLED, TrancheState.LAPSED):
        return None

    termination = grant.termination
    rule = policy[termination.type] if termination else None

    def move(to: TrancheState, reason: str) -> Transition:
        return Transition(at=now, from_state=state, to_state=to, reason=reason)

    # 过失解除:全部未结算批次立即作废
    if rule and rule.forfeit_vested_unsettled:
        record.lapse_reason = "过失解除,未结算批次全部作废"
        return move(TrancheState.LAPSED, record.lapse_reason)

    # 行权窗口届满(仅离职后)
    if termination is not None and rule is not None:
        deadline = exercise_deadline(grant, rule, blackouts)
        record.exercise_deadline = deadline
        if deadline is not None and now > deadline:
            record.lapse_reason = "离职行权窗口届满"
            return move(TrancheState.LAPSED, record.lapse_reason)

    service_end = termination.date if termination else None
    adjusted = adjusted_vest_date(
        record.terms.base_vest_date, grant.service_start, grant.leaves, service_end
    )
    record.adjusted_vest_date = adjusted

    if state == TrancheState.PENDING:
        return _evaluate_pending(grant, record, adjusted, now, rule, move)
    if state == TrancheState.LOCKED:
        if approvals_complete and not in_blackout(now, blackouts):
            return move(TrancheState.EXERCISABLE, "审批齐全且不在黑窗期")
        return None
    if state == TrancheState.EXERCISABLE:
        if in_blackout(now, blackouts):
            return move(TrancheState.LOCKED, "进入黑窗期,暂停执行")
        if not approvals_complete:
            return move(TrancheState.LOCKED, "数据变更导致审批确认失效,待重新确认")
        return None
    return None


def _evaluate_pending(grant, record, adjusted, now, rule, move) -> Transition | None:
    terms = record.terms
    termination = grant.termination

    # 绩效评定:有绩效要求的批次必须先有评定记录
    payout: Decimal | None = None
    achievement: Decimal | None = None
    if terms.performance_metric is None:
        payout = Decimal("1")
    else:
        assessment = grant.assessments.get(terms.performance_metric)
        if assessment is not None:
            achievement = achievement_ratio(assessment.actual, assessment.target)
            payout = payout_ratio(terms.payout_curve, achievement)

    # 归属日晚于离职日:按政策加速或作废
    if termination is not None and rule is not None and adjusted > termination.date:
        if rule.acceleration_ratio > 0 and payout is not None:
            record.achievement = achievement
            record.payout = payout
            record.adjusted_vest_date = termination.date
            record.eligible_shares = quantize_shares(
                terms.scheduled_shares * rule.acceleration_ratio * payout
            )
            if record.eligible_shares <= 0:
                record.lapse_reason = "加速后归属数量为零"
                return move(TrancheState.LAPSED, record.lapse_reason)
            return move(
                TrancheState.LOCKED,
                f"离职加速归属,比例 {rule.acceleration_ratio}",
            )
        record.lapse_reason = "归属日晚于离职日,未归属批次作废"
        return move(TrancheState.LAPSED, record.lapse_reason)

    # 正常归属:业务日期到达实际归属日且绩效已评定
    vest_reached = local_date(now, grant.timezone) >= adjusted
    if not vest_reached or payout is None:
        return None
    record.achievement = achievement
    record.payout = payout
    if payout <= 0:
        record.lapse_reason = "绩效未达标,归属比例为零"
        return move(TrancheState.LAPSED, record.lapse_reason)
    record.eligible_shares = quantize_shares(terms.scheduled_shares * payout)
    return move(TrancheState.LOCKED, "服务期与绩效条件已满足")
