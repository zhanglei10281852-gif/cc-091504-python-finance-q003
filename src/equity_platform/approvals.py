"""三方复核：人事、薪资、证券管理各自确认自己负责的数据。

规则：
- 批次需三方在当前版本全部确认后方可执行；
- 任何一方退回，批次回到“已退回”，原意见与差异（记录值 vs 复核方认定值）永久保留；
- 修正后重新提交，版本号递增，历史复核记录不清除。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .errors import WorkflowError


class Role(str, Enum):
    HR = "HR"                    # 人事：服务期间、休假、离职事实
    PAYROLL = "PAYROLL"          # 薪资：税务身份、预扣口径
    STOCK_ADMIN = "STOCK_ADMIN"  # 证券管理：授予、价格、黑窗与交割


class Decision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class BatchStatus(str, Enum):
    DRAFT = "DRAFT"          # 草稿
    IN_REVIEW = "IN_REVIEW"  # 复核中
    APPROVED = "APPROVED"    # 三方已确认
    RETURNED = "RETURNED"    # 已退回（保留意见与差异）
    EXECUTED = "EXECUTED"    # 已执行


@dataclass(frozen=True)
class FieldDiff:
    """复核方认定的差异：字段、系统记录值、复核方认定值。"""

    field: str
    recorded: str
    expected: str


@dataclass(frozen=True)
class ReviewRecord:
    role: Role
    decision: Decision
    comment: str
    differences: tuple[FieldDiff, ...]
    batch_version: int
    created_at: datetime


@dataclass
class SettlementBatch:
    batch_id: str
    tranche_ids: tuple[str, ...]
    created_at: datetime
    version: int = 1
    status: BatchStatus = BatchStatus.DRAFT
    reviews: list[ReviewRecord] = field(default_factory=list)
    idempotency_key: str | None = None

    def submit(self) -> None:
        if self.status is not BatchStatus.DRAFT:
            raise WorkflowError(f"结算批次 {self.batch_id} 状态为 {self.status.value}，不能提交复核")
        self.status = BatchStatus.IN_REVIEW

    def resubmit(self) -> None:
        if self.status is not BatchStatus.RETURNED:
            raise WorkflowError(f"结算批次 {self.batch_id} 未被退回，不能重新提交")
        self.version += 1
        self.status = BatchStatus.IN_REVIEW

    def approvals(self) -> set[Role]:
        return {
            r.role
            for r in self.reviews
            if r.batch_version == self.version and r.decision is Decision.APPROVED
        }

    def missing_roles(self) -> set[Role]:
        return set(Role) - self.approvals()

    def record_review(
        self,
        *,
        role: Role,
        decision: Decision,
        comment: str,
        differences: tuple[FieldDiff, ...],
        at: datetime,
    ) -> None:
        if self.status is not BatchStatus.IN_REVIEW:
            raise WorkflowError(f"结算批次 {self.batch_id} 状态为 {self.status.value}，不能复核")
        self.reviews.append(
            ReviewRecord(role, decision, comment, differences, self.version, at)
        )
        if decision is Decision.REJECTED:
            self.status = BatchStatus.RETURNED
        elif not self.missing_roles():
            self.status = BatchStatus.APPROVED
