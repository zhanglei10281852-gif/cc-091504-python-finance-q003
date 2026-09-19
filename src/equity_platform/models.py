"""领域模型：授予协议（多版本）、归属批次、休假、离职、黑窗期、价格与税务身份。

约定：
- 所有数量与金额一律使用 Decimal，禁止浮点进入领域层；
- 所有“时刻”使用带时区的 datetime（内部按 UTC 比较），所有“日期”为协议时区自然日；
- 归属批次状态机：待满足 PENDING → 锁定 LOCKED → 可执行 RELEASABLE → 已结算 SETTLED，
  任一未终结状态在满足失效条件时进入 失效 FORFEITED。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免与 settlement 模块循环引用
    from .settlement import SettlementQuote


class TrancheStatus(str, Enum):
    PENDING = "PENDING"          # 待满足：服务或绩效条件尚未满足
    LOCKED = "LOCKED"            # 锁定：条件已满足，但受黑窗期等限制暂不可结算
    RELEASABLE = "RELEASABLE"    # 可执行：可加入结算批次并执行
    SETTLED = "SETTLED"          # 已结算：结果封存，不得倒改
    FORFEITED = "FORFEITED"      # 失效


class SettlementMethod(str, Enum):
    GROSS = "gross"                  # 全额交付，税款由员工现金补缴
    NET_SHARE = "net_share"          # 净股交付：公司代扣股份抵税
    SELL_TO_COVER = "sell_to_cover"  # 卖股缴税：卖出部分股份覆盖税款


class TerminationType(str, Enum):
    VOLUNTARY = "voluntary"
    INVOLUNTARY = "involuntary"
    FOR_CAUSE = "for_cause"
    RETIREMENT = "retirement"
    DEATH_DISABILITY = "death_disability"


@dataclass(frozen=True)
class DateRange:
    """左闭右开的自然日区间。"""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("日期区间终点早于起点")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    def overlap_days(self, other: "DateRange") -> int:
        latest = max(self.start, other.start)
        earliest = min(self.end, other.end)
        return max(0, (earliest - latest).days)


@dataclass(frozen=True)
class PerformanceCondition:
    """绩效条件：达成率 = 实际值 / 目标值；低于阈值整体失效，高于上限按上限计。"""

    metric: str
    target: Decimal
    threshold: Decimal = Decimal("0")
    cap: Decimal = Decimal("1")

    def factor(self, actual: Decimal) -> Decimal:
        if self.target <= 0:
            raise ValueError("绩效目标必须为正数")
        ratio = actual / self.target
        if ratio < self.threshold:
            return Decimal("0")
        return min(ratio, self.cap)


@dataclass(frozen=True)
class TrancheSpec:
    """协议中的一个归属批次约定。"""

    sequence: int
    required_service_days: int
    shares: Decimal
    performance: PerformanceCondition | None = None


@dataclass(frozen=True)
class AgreementTerms:
    """协议条款的一个版本。修订只能追加新版本，历史版本永不改写。"""

    version: int
    effective_from: date
    grant_date: date
    cutoff_timezone: str
    tranches: tuple[TrancheSpec, ...]
    settlement_method: SettlementMethod
    post_termination_window_days: int

    def spec_for(self, sequence: int) -> TrancheSpec | None:
        for spec in self.tranches:
            if spec.sequence == sequence:
                return spec
        return None


@dataclass
class Grant:
    grant_id: str
    employee_id: str
    symbol: str
    terms_history: list[AgreementTerms]

    def terms_as_of(self, day: date) -> AgreementTerms:
        """返回某日生效的协议版本（向后版本：生效日未到的新版本不参与计算）。"""
        effective = [t for t in self.terms_history if t.effective_from <= day]
        if not effective:
            raise ValueError(f"授予 {self.grant_id} 在 {day.isoformat()} 尚无生效版本")
        return effective[-1]


@dataclass(frozen=True)
class LeavePeriod:
    """停薪休假：suspends_vesting 为真时休假期间暂停归属计时。"""

    leave_id: str
    employee_id: str
    period: DateRange
    suspends_vesting: bool = True


@dataclass(frozen=True)
class ServicePeriod:
    employee_id: str
    period: DateRange


@dataclass(frozen=True)
class TaxIdentity:
    employee_id: str
    tax_residency: str
    withholding_rate: Decimal
    tax_currency: str


@dataclass(frozen=True)
class MarketPrice:
    symbol: str
    price_date: date
    currency: str
    price: Decimal


@dataclass(frozen=True)
class FxRate:
    """汇率：1 单位 base = rate 单位 quote。"""

    base: str
    quote: str
    rate_date: date
    rate: Decimal


@dataclass(frozen=True)
class BlackoutWindow:
    window_id: str
    period: DateRange
    reason: str = ""


@dataclass(frozen=True)
class TerminationPolicy:
    """离职处理策略：未归属是否失效/加速、已归属未结算是否失效、结算窗口天数。"""

    forfeit_unvested: bool
    accelerate_unvested: bool
    forfeit_releasable: bool
    window_days: int


DEFAULT_TERMINATION_POLICIES: dict[TerminationType, TerminationPolicy] = {
    TerminationType.VOLUNTARY: TerminationPolicy(True, False, False, 90),
    TerminationType.INVOLUNTARY: TerminationPolicy(True, False, False, 180),
    TerminationType.RETIREMENT: TerminationPolicy(True, False, False, 180),
    TerminationType.DEATH_DISABILITY: TerminationPolicy(False, True, False, 365),
    TerminationType.FOR_CAUSE: TerminationPolicy(True, False, True, 0),
}


@dataclass
class Termination:
    employee_id: str
    termination_type: TerminationType
    termination_date: date
    policy: TerminationPolicy
    recorded_at: datetime


@dataclass(frozen=True)
class TrancheEvent:
    """批次状态变更留痕。"""

    at: datetime
    from_status: TrancheStatus | None
    to_status: TrancheStatus
    reason: str


@dataclass
class Tranche:
    """归属批次实例。已归属（锁定/可执行）与已结算批次的数量与归属日被冻结，
    后续资料补录或协议修订不再影响；待满足批次始终按最新资料重算。"""

    tranche_id: str
    grant_id: str
    employee_id: str
    sequence: int
    spec_shares: Decimal
    required_service_days: int
    cutoff_timezone: str
    terms_version: int
    performance: PerformanceCondition | None = None
    status: TrancheStatus = TrancheStatus.PENDING
    vest_date: date | None = None
    performance_factor: Decimal | None = None
    vested_shares: Decimal | None = None
    forfeited_shares: Decimal = Decimal("0")
    settlement_deadline: date | None = None
    settlement: "SettlementRecord | None" = None
    events: list[TrancheEvent] = field(default_factory=list)

    def transition(self, to: TrancheStatus, at: datetime, reason: str) -> None:
        self.events.append(TrancheEvent(at, self.status, to, reason))
        self.status = to


@dataclass(frozen=True)
class SettlementRecord:
    """已结算批次的不可变快照：协议版本、价格、汇率、税额与到账构成一并封存。"""

    settlement_id: str
    tranche_id: str
    grant_id: str
    employee_id: str
    terms_version: int
    method: SettlementMethod
    quote: "SettlementQuote"
    price_date: date
    settled_at: datetime
    idempotency_key: str
    ledger_entry_ids: tuple[str, ...]


@dataclass(frozen=True)
class Receipt:
    """外部系统（券商/薪资/税务）回执。"""

    tranche_id: str
    channel: str
    ok: bool
    detail: str
    received_at: datetime
