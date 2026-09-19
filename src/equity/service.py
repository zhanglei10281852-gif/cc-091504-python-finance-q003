"""EquityService:平台门面,组合归属引擎、结算引擎、审批、仓储与网关。

关键保证:
- 协议修订只追加新版本且生效日不早于提出日;已结算批次条款逐字校验,不得倒改;
- 批次归属时锁定管辖版本,后续修订不影响已归属/已结算批次;
- 结算幂等:同一幂等键重复提交返回原回执;并发下状态检查与扣减在同一临界区;
- 网关失败只产生失败回执,不入台账、不改状态,修正后可换新幂等键重试。
"""

from __future__ import annotations

import itertools
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .approvals import is_complete, snapshot_hash, submit_review
from .constants import DEFAULT_TIMEZONE
from .enums import (
    ReceiptStatus,
    ReviewDecision,
    Role,
    SettlementMode,
    TrancheState,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .gateway import DepositoryGateway
from .models import (
    AgreementVersion,
    ApprovalCase,
    BlackoutWindow,
    FxRatePoint,
    Grant,
    LeavePeriod,
    PerformanceAssessment,
    PricePoint,
    ReviewRecord,
    TaxProfile,
    Termination,
    TrancheRecord,
    TrancheTerms,
    Transition,
)
from .settlement import SettlementBreakdown, compute_settlement
from .store import LedgerEntry, Receipt, Store
from .timeutil import end_of_day_utc, ensure_utc, local_date, utcnow
from .vesting import (
    DEFAULT_TERMINATION_POLICY,
    adjusted_vest_date,
    evaluate_tranche,
)

_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}-{next(_counter):06d}"


class EquityService:
    def __init__(
        self,
        runtime_dir: str | Path | None = None,
        gateway: DepositoryGateway | None = None,
        termination_policy=None,
    ):
        self.store = Store(runtime_dir)
        self.gateway = gateway or DepositoryGateway()
        self.policy = termination_policy or DEFAULT_TERMINATION_POLICY

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------

    def get_grant(self, grant_id: str) -> Grant:
        grant = self.store.state.grants.get(grant_id)
        if grant is None:
            raise NotFoundError(f"授予不存在: {grant_id}")
        return grant

    def find_tranche(self, tranche_id: str) -> tuple[Grant, TrancheRecord]:
        for grant in self.store.state.grants.values():
            record = grant.tranches.get(tranche_id)
            if record is not None:
                return grant, record
        raise NotFoundError(f"批次不存在: {tranche_id}")

    # ------------------------------------------------------------------
    # 授予与协议版本
    # ------------------------------------------------------------------

    def create_grant(
        self,
        *,
        grant_id: str,
        employee_id: str,
        award_type: str,
        stock_currency: str,
        service_start,
        settlement_mode: SettlementMode,
        tranches: list[TrancheTerms],
        effective_from,
        timezone: str = DEFAULT_TIMEZONE,
        note: str = "初始授予",
        now: datetime | None = None,
    ) -> Grant:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            if grant_id in self.store.state.grants:
                raise ConflictError(f"授予编号已存在: {grant_id}", code="duplicate_id")
            version = AgreementVersion(
                version=1,
                effective_from=effective_from,
                settlement_mode=settlement_mode,
                tranches=tuple(tranches),
                note=note,
                created_at=now,
            )
            grant = Grant(
                grant_id=grant_id,
                employee_id=employee_id,
                award_type=award_type,
                stock_currency=stock_currency,
                service_start=service_start,
                timezone=timezone,
                versions=[version],
                tranches={
                    t.tranche_id: TrancheRecord(terms=t) for t in tranches
                },
            )
            self.store.state.grants[grant_id] = grant
            self.store.emit("grant_created", {"grant_id": grant_id, "employee_id": employee_id})
            return grant

    def amend_grant(
        self,
        grant_id: str,
        *,
        effective_from,
        settlement_mode: SettlementMode,
        tranches: list[TrancheTerms],
        note: str,
        now: datetime | None = None,
    ) -> AgreementVersion:
        """协议修订:只追加新版本,向后生效;已结算批次条款不得变更。"""
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant = self.get_grant(grant_id)
            today = local_date(now, grant.timezone)
            if effective_from < today:
                raise ValidationError(
                    "协议修订只能向后生效,生效日不得早于提出日",
                    code="retroactive_amendment",
                )
            new_terms = {t.tranche_id: t for t in tranches}
            for tranche_id, record in grant.tranches.items():
                if tranche_id not in new_terms:
                    raise ValidationError(
                        f"修订必须完整列示批次 {tranche_id},不得缺漏",
                        code="tranche_missing",
                    )
                if record.state == TrancheState.SETTLED and new_terms[tranche_id] != record.terms:
                    raise ConflictError(
                        f"批次 {tranche_id} 已结算,不得倒改其条款",
                        code="settled_tranche_immutable",
                    )
            version = AgreementVersion(
                version=max(v.version for v in grant.versions) + 1,
                effective_from=effective_from,
                settlement_mode=settlement_mode,
                tranches=tuple(tranches),
                note=note,
                created_at=now,
            )
            grant.versions.append(version)
            for tranche_id, terms in new_terms.items():
                if tranche_id not in grant.tranches:
                    grant.tranches[tranche_id] = TrancheRecord(terms=terms)
            self.store.emit(
                "grant_amended",
                {"grant_id": grant_id, "version": version.version, "note": note},
            )
            self._evaluate(grant, now)
            return version

    # ------------------------------------------------------------------
    # 事实登记(各岗位维护的数据)
    # ------------------------------------------------------------------

    def record_leave(self, grant_id: str, leave: LeavePeriod, *, now=None) -> None:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant = self.get_grant(grant_id)
            if leave.start > leave.end:
                raise ValidationError("休假开始日晚于结束日", code="invalid_leave")
            grant.leaves.append(leave)
            self.store.emit(
                "leave_recorded",
                {"grant_id": grant_id, "start": str(leave.start), "end": str(leave.end)},
            )
            self._evaluate(grant, now)

    def assess_performance(
        self, grant_id: str, assessment: PerformanceAssessment, *, now=None
    ) -> None:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant = self.get_grant(grant_id)
            grant.assessments[assessment.metric] = assessment
            self.store.emit(
                "performance_assessed",
                {"grant_id": grant_id, "metric": assessment.metric},
            )
            self._evaluate(grant, now)

    def record_termination(self, grant_id: str, termination: Termination, *, now=None) -> None:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant = self.get_grant(grant_id)
            if grant.termination is not None:
                raise ConflictError("离职事实已登记,如需更正请先撤销", code="termination_exists")
            grant.termination = termination
            self.store.emit(
                "termination_recorded",
                {"grant_id": grant_id, "type": termination.type.value, "date": str(termination.date)},
            )
            self._evaluate(grant, now)

    def set_tax_profile(self, profile: TaxProfile) -> None:
        with self.store.lock:
            self.store.state.tax_profiles[profile.employee_id] = profile
            self.store.emit("tax_profile_set", {"employee_id": profile.employee_id})

    def add_price(self, point: PricePoint) -> None:
        with self.store.lock:
            self.store.add_price(point)
            self.store.emit("price_added", {"currency": point.currency, "at": str(point.at)})

    def add_fx(self, rate: FxRatePoint) -> None:
        with self.store.lock:
            self.store.add_fx(rate)
            self.store.emit("fx_added", {"base": rate.base, "quote": rate.quote, "at": str(rate.at)})

    def add_blackout(self, window: BlackoutWindow) -> None:
        with self.store.lock:
            if ensure_utc(window.start) >= ensure_utc(window.end):
                raise ValidationError("黑窗期开始必须早于结束", code="invalid_blackout")
            self.store.state.blackouts.append(window)
            self.store.emit("blackout_added", {"reason": window.reason})

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------

    def _role_snapshot(self, role: Role, grant: Grant, record: TrancheRecord) -> dict:
        if role == Role.HR:
            return {
                "service_start": str(grant.service_start),
                "leaves": [
                    {"start": str(l.start), "end": str(l.end), "days": l.days}
                    for l in grant.leaves
                ],
                "termination": (
                    {
                        "type": grant.termination.type.value,
                        "date": str(grant.termination.date),
                        "exercise_window_days": grant.termination.exercise_window_days,
                    }
                    if grant.termination
                    else None
                ),
            }
        if role == Role.PAYROLL:
            profile = self.store.state.tax_profiles.get(grant.employee_id)
            return {
                "tax_profile": (
                    {
                        "jurisdiction": profile.jurisdiction,
                        "residency": profile.residency,
                        "tax_currency": profile.tax_currency,
                        "brackets": [
                            {"lower": str(b.lower), "rate": str(b.rate)}
                            for b in profile.brackets
                        ],
                    }
                    if profile
                    else None
                )
            }
        # SECURITIES:协议版本、批次条款与数量
        version = (
            grant.versions[record.governing_version - 1]
            if record.governing_version
            else grant.current_version()
        )
        return {
            "governing_version": record.governing_version,
            "settlement_mode": version.settlement_mode.value,
            "tranche": {
                "tranche_id": record.terms.tranche_id,
                "scheduled_shares": str(record.terms.scheduled_shares),
                "base_vest_date": str(record.terms.base_vest_date),
            },
            "adjusted_vest_date": (
                str(record.adjusted_vest_date) if record.adjusted_vest_date else None
            ),
            "eligible_shares": (
                str(record.eligible_shares) if record.eligible_shares is not None else None
            ),
        }

    def _expected_hashes(self, grant: Grant, record: TrancheRecord) -> dict[Role, str]:
        return {
            role: snapshot_hash(self._role_snapshot(role, grant, record))
            for role in Role
        }

    def _approvals_complete(self, grant: Grant, record: TrancheRecord) -> bool:
        case = self.store.state.approval_cases.get(record.terms.tranche_id)
        if case is None:
            return False
        return is_complete(case, self._expected_hashes(grant, record))

    def review(
        self,
        tranche_id: str,
        *,
        role: Role,
        decision: ReviewDecision,
        opinion: str = "",
        now: datetime | None = None,
    ) -> ReviewRecord:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant, record = self.find_tranche(tranche_id)
            if record.state not in (TrancheState.LOCKED, TrancheState.EXERCISABLE):
                raise ValidationError(
                    f"批次当前状态 {record.state.value},尚无可审批的归属结果",
                    code="not_ready_for_review",
                )
            case = self.store.state.approval_cases.get(tranche_id)
            if case is None:
                case = ApprovalCase(
                    case_id=_next_id("CASE"), grant_id=grant.grant_id, tranche_id=tranche_id
                )
                self.store.state.approval_cases[tranche_id] = case
            snapshot = self._role_snapshot(role, grant, record)
            record_review = submit_review(
                case,
                role=role,
                decision=decision,
                opinion=opinion,
                snapshot=snapshot,
                at=now,
            )
            self.store.emit(
                "review_submitted",
                {
                    "tranche_id": tranche_id,
                    "role": role.value,
                    "decision": decision.value,
                },
            )
            self._evaluate(grant, now)
            return record_review

    # ------------------------------------------------------------------
    # 状态评估
    # ------------------------------------------------------------------

    def evaluate_grant(self, grant_id: str, *, now: datetime | None = None) -> list[Transition]:
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            grant = self.get_grant(grant_id)
            return self._evaluate(grant, now)

    def _evaluate(self, grant: Grant, now: datetime) -> list[Transition]:
        transitions: list[Transition] = []
        for record in grant.tranches.values():
            if record.state == TrancheState.PENDING:
                self._sync_terms_to_applicable_version(grant, record)
            transition = evaluate_tranche(
                grant,
                record,
                now,
                self.store.state.blackouts,
                approvals_complete=self._approvals_complete(grant, record),
                policy=self.policy,
            )
            if transition is None:
                continue
            record.state = transition.to_state
            record.history.append(transition)
            if transition.to_state == TrancheState.LOCKED:
                record.governing_version = grant.version_at(record.adjusted_vest_date).version
            self.store.emit(
                "tranche_transition",
                {
                    "grant_id": grant.grant_id,
                    "tranche_id": record.terms.tranche_id,
                    "from": transition.from_state.value,
                    "to": transition.to_state.value,
                    "reason": transition.reason,
                },
            )
            transitions.append(transition)
        return transitions

    def _sync_terms_to_applicable_version(self, grant: Grant, record: TrancheRecord) -> None:
        """PENDING 批次按"实际归属日有效版本"确定管辖条款,迭代至稳定。"""
        terms = record.terms
        service_end = grant.termination.date if grant.termination else None
        for _ in range(len(grant.versions) + 1):
            adjusted = adjusted_vest_date(
                terms.base_vest_date, grant.service_start, grant.leaves, service_end
            )
            version = grant.version_at(adjusted)
            version_terms = next(
                (t for t in version.tranches if t.tranche_id == terms.tranche_id), None
            )
            if version_terms is None or version_terms == terms:
                break
            terms = version_terms
        record.terms = terms

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------

    def settle(
        self,
        tranche_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> Receipt:
        """发起结算。幂等:同一 idempotency_key 重复提交返回原回执。"""
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            existing = self.store.state.idempotency.get(idempotency_key)
            if existing is not None:
                return self.store.state.receipts[existing]
            grant, record = self.find_tranche(tranche_id)
            return self._execute_settlement(grant, record, idempotency_key, 1, now)

    def retry_receipt(
        self,
        receipt_id: str,
        *,
        new_idempotency_key: str,
        now: datetime | None = None,
    ) -> Receipt:
        """失败回执重试:换新幂等键,原失败回执保留可查。"""
        now = ensure_utc(now) if now else utcnow()
        with self.store.lock:
            receipt = self.store.state.receipts.get(receipt_id)
            if receipt is None:
                raise NotFoundError(f"回执不存在: {receipt_id}")
            if receipt.status != ReceiptStatus.FAILED:
                raise ConflictError("仅失败回执可重试", code="receipt_not_failed")
            existing = self.store.state.idempotency.get(new_idempotency_key)
            if existing is not None:
                return self.store.state.receipts[existing]
            grant, record = self.find_tranche(receipt.tranche_id)
            return self._execute_settlement(
                grant, record, new_idempotency_key, receipt.attempt + 1, now
            )

    def _execute_settlement(
        self,
        grant: Grant,
        record: TrancheRecord,
        idempotency_key: str,
        attempt: int,
        now: datetime,
    ) -> Receipt:
        self._evaluate(grant, now)
        if record.state == TrancheState.SETTLED:
            raise ConflictError(
                f"批次已结算(回执 {record.receipt_id}),不得重复扣股或付款",
                code="already_settled",
            )
        if record.state != TrancheState.EXERCISABLE:
            raise ConflictError(
                f"批次状态 {record.state.value},不可结算;需审批齐全且不在黑窗期",
                code="not_exercisable",
            )

        breakdown = self._build_breakdown(grant, record)
        cash_items = self._cash_items(breakdown)
        ack = self.gateway.transfer(
            employee_id=grant.employee_id,
            idempotency_key=idempotency_key,
            shares=str(breakdown.net_shares),
            cash_items=cash_items,
        )
        receipt = Receipt(
            receipt_id=_next_id("RCPT"),
            idempotency_key=idempotency_key,
            grant_id=grant.grant_id,
            tranche_id=record.terms.tranche_id,
            employee_id=grant.employee_id,
            status=ReceiptStatus.COMPLETED if ack.ok else ReceiptStatus.FAILED,
            version=record.governing_version or grant.current_version().version,
            attempt=attempt,
            breakdown=breakdown if ack.ok else None,
            failure_reason=None if ack.ok else ack.failure_reason,
            confirmation_no=ack.confirmation_no,
            created_at=now,
        )
        # 先入库再发事件,保证事件落盘时回执与幂等键已在快照内
        self.store.state.receipts[receipt.receipt_id] = receipt
        self.store.state.idempotency[idempotency_key] = receipt.receipt_id
        if ack.ok:
            record.state = TrancheState.SETTLED
            record.receipt_id = receipt.receipt_id
            record.history.append(
                Transition(
                    at=now,
                    from_state=TrancheState.EXERCISABLE,
                    to_state=TrancheState.SETTLED,
                    reason=f"结算完成,回执 {receipt.receipt_id}",
                )
            )
            self._post_ledger(receipt, breakdown)
            self.store.emit(
                "settlement_completed",
                {"tranche_id": record.terms.tranche_id, "receipt_id": receipt.receipt_id},
            )
        else:
            self.store.emit(
                "settlement_failed",
                {
                    "tranche_id": record.terms.tranche_id,
                    "receipt_id": receipt.receipt_id,
                    "reason": ack.failure_reason,
                },
            )
        return receipt

    def _build_breakdown(self, grant: Grant, record: TrancheRecord) -> SettlementBreakdown:
        if record.eligible_shares is None or record.adjusted_vest_date is None:
            raise ConflictError("批次尚未归属,无结算数量", code="not_vested")
        profile = self.store.state.tax_profiles.get(grant.employee_id)
        if profile is None:
            raise ValidationError(
                f"员工 {grant.employee_id} 缺少税务身份资料", code="tax_profile_missing"
            )
        version = (
            grant.versions[record.governing_version - 1]
            if record.governing_version
            else grant.current_version()
        )
        as_of = end_of_day_utc(record.adjusted_vest_date, grant.timezone)
        price_point = self.store.price_book.as_of(as_of, grant.stock_currency)
        fx_rate = fx_at = None
        if profile.tax_currency != grant.stock_currency:
            fx_point = self.store.fx_book.as_of(
                as_of, grant.stock_currency, profile.tax_currency
            )
            fx_rate, fx_at = fx_point.rate, fx_point.at
        return compute_settlement(
            mode=version.settlement_mode,
            gross_shares=record.eligible_shares,
            price=price_point.price,
            price_currency=grant.stock_currency,
            price_at=price_point.at,
            fx_rate=fx_rate,
            fx_at=fx_at,
            tax_currency=profile.tax_currency,
            brackets=profile.brackets,
        )

    @staticmethod
    def _cash_items(breakdown: SettlementBreakdown) -> list[dict]:
        items = []
        for purpose, amount in (
            ("fractional_cash", breakdown.fractional_cash_tax_ccy),
            ("sell_residual", breakdown.sell_residual_tax_ccy),
            ("withhold_refund", breakdown.withhold_refund_tax_ccy),
        ):
            if amount > 0:
                items.append(
                    {
                        "purpose": purpose,
                        "amount": str(amount),
                        "currency": breakdown.tax_currency,
                    }
                )
        return items

    def _post_ledger(self, receipt: Receipt, breakdown: SettlementBreakdown) -> None:
        now = receipt.created_at
        entries = [
            LedgerEntry(
                entry_id=_next_id("LED"),
                receipt_id=receipt.receipt_id,
                grant_id=receipt.grant_id,
                tranche_id=receipt.tranche_id,
                kind="SHARE_DEBIT",
                quantity=breakdown.whole_shares,
                currency=breakdown.price_currency,
                at=now,
            ),
            LedgerEntry(
                entry_id=_next_id("LED"),
                receipt_id=receipt.receipt_id,
                grant_id=receipt.grant_id,
                tranche_id=receipt.tranche_id,
                kind="TAX_REMITTANCE",
                quantity=breakdown.tax_due_tax_ccy,
                currency=breakdown.tax_currency,
                at=now,
            ),
        ]
        cash_out = (
            breakdown.fractional_cash_tax_ccy
            + breakdown.sell_residual_tax_ccy
            + breakdown.withhold_refund_tax_ccy
        )
        if cash_out > 0:
            entries.append(
                LedgerEntry(
                    entry_id=_next_id("LED"),
                    receipt_id=receipt.receipt_id,
                    grant_id=receipt.grant_id,
                    tranche_id=receipt.tranche_id,
                    kind="CASH_PAYOUT",
                    quantity=cash_out,
                    currency=breakdown.tax_currency,
                    at=now,
                )
            )
        if breakdown.employee_cash_due_tax_ccy > 0:
            entries.append(
                LedgerEntry(
                    entry_id=_next_id("LED"),
                    receipt_id=receipt.receipt_id,
                    grant_id=receipt.grant_id,
                    tranche_id=receipt.tranche_id,
                    kind="CASH_DUE",
                    quantity=breakdown.employee_cash_due_tax_ccy,
                    currency=breakdown.tax_currency,
                    at=now,
                )
            )
        self.store.state.ledger.extend(entries)

    # ------------------------------------------------------------------
    # 台账核对
    # ------------------------------------------------------------------

    def ledger_entries(self, grant_id: str | None = None) -> list[LedgerEntry]:
        entries = self.store.state.ledger
        if grant_id is None:
            return list(entries)
        return [e for e in entries if e.grant_id == grant_id]

    def shares_debited(self, tranche_id: str) -> Decimal:
        return sum(
            (e.quantity for e in self.store.state.ledger
             if e.tranche_id == tranche_id and e.kind == "SHARE_DEBIT"),
            Decimal("0"),
        )
