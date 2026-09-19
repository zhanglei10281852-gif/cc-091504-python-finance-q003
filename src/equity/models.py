"""领域事实模型。

约定:
- 协议以 AgreementVersion 表示,修订只追加新版本,生效日不得早于提出日;
- TrancheTerms 是协议层面的批次条款(不可变);TrancheRecord 是运行态;
- 批次归属时锁定 governing_version,之后协议修订不影响已归属/已结算批次;
- 所有金额与股数使用 Decimal,舍入规则见 constants。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .codec import register
from .enums import ReviewDecision, Role, SettlementMode, TerminationType, TrancheState


@register
@dataclass(frozen=True)
class PayoutStep:
    """绩效阶梯:达成率 >= threshold 时归属比例 ratio。"""

    threshold: Decimal
    ratio: Decimal


@register
@dataclass(frozen=True)
class PerformanceAssessment:
    metric: str
    target: Decimal
    actual: Decimal
    assessed_at: datetime


@register
@dataclass(frozen=True)
class TrancheTerms:
    tranche_id: str
    sequence: int
    scheduled_shares: Decimal
    base_vest_date: date
    performance_metric: str | None = None
    payout_curve: tuple[PayoutStep, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "payout_curve", tuple(self.payout_curve))


@register
@dataclass(frozen=True)
class AgreementVersion:
    """协议版本。version 从 1 递增;effective_from 决定其管辖的归属事件。"""

    version: int
    effective_from: date
    settlement_mode: SettlementMode
    tranches: tuple[TrancheTerms, ...]
    note: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "tranches", tuple(self.tranches))


@register
@dataclass(frozen=True)
class LeavePeriod:
    """停薪休假,闭区间 [start, end],休假期间归属计时暂停。"""

    start: date
    end: date
    reason: str = ""

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@register
@dataclass(frozen=True)
class Termination:
    type: TerminationType
    date: date
    exercise_window_days: int | None = None  # None 时取离职政策默认


@register
@dataclass(frozen=True)
class TaxBracket:
    """累进档:应纳税所得额区间 [lower, 下一档 lower) 适用 rate。"""

    lower: Decimal
    rate: Decimal


@register
@dataclass(frozen=True)
class TaxProfile:
    employee_id: str
    jurisdiction: str
    residency: str
    tax_currency: str
    brackets: tuple[TaxBracket, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "brackets", tuple(self.brackets))


@register
@dataclass(frozen=True)
class BlackoutWindow:
    """黑窗期,半开区间 [start, end),UTC 时刻。期内禁止执行结算。"""

    start: datetime
    end: datetime
    reason: str = ""


@register
@dataclass(frozen=True)
class PricePoint:
    at: datetime
    currency: str
    price: Decimal


@register
@dataclass(frozen=True)
class FxRatePoint:
    at: datetime
    base: str   # 股票币种
    quote: str  # 税务币种
    rate: Decimal  # 1 base = rate quote


@register
@dataclass(frozen=True)
class Transition:
    at: datetime
    from_state: TrancheState
    to_state: TrancheState
    reason: str


@register
@dataclass
class TrancheRecord:
    """批次运行态。governing_version 在归属(转 LOCKED)时锁定。"""

    terms: TrancheTerms
    state: TrancheState = TrancheState.PENDING
    governing_version: int | None = None
    adjusted_vest_date: date | None = None
    achievement: Decimal | None = None
    payout: Decimal | None = None
    eligible_shares: Decimal | None = None
    lapse_reason: str | None = None
    exercise_deadline: datetime | None = None
    receipt_id: str | None = None
    history: list[Transition] = field(default_factory=list)


@register
@dataclass
class Grant:
    grant_id: str
    employee_id: str
    award_type: str
    stock_currency: str
    service_start: date
    timezone: str
    versions: list[AgreementVersion]
    tranches: dict[str, TrancheRecord]
    leaves: list[LeavePeriod] = field(default_factory=list)
    assessments: dict[str, PerformanceAssessment] = field(default_factory=dict)
    termination: Termination | None = None

    def current_version(self) -> AgreementVersion:
        return self.versions[-1]

    def version_at(self, d: date) -> AgreementVersion:
        """业务日期 d 有效的协议版本:生效日 <= d 的最新版本。"""
        applicable = [v for v in self.versions if v.effective_from <= d]
        if not applicable:
            return self.versions[0]
        return max(applicable, key=lambda v: (v.effective_from, v.version))


@register
@dataclass(frozen=True)
class ReviewRecord:
    """审批留痕。append-only,退回的原意见永久保留;

    退回后重新确认时,diff_from_previous 记录退回快照与当前快照的字段级差异。
    """

    role: Role
    decision: ReviewDecision
    opinion: str
    snapshot_hash: str
    snapshot: dict
    diff_from_previous: dict | None
    at: datetime


@register
@dataclass
class ApprovalCase:
    case_id: str
    grant_id: str
    tranche_id: str
    reviews: list[ReviewRecord] = field(default_factory=list)

    def latest_by_role(self) -> dict[Role, ReviewRecord]:
        latest: dict[Role, ReviewRecord] = {}
        for rec in self.reviews:
            latest[rec.role] = rec
        return latest
