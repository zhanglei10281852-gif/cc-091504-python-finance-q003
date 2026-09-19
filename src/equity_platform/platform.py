"""股权激励归属与结算平台：人事、薪资、证券管理三方共用同一套口径。

设计要点：
- 所有计算依赖的时间显式传入（as_of / at），结果可重现、可审计；
- 协议修订只追加新版本且只能向后生效；已结算批次封存，已归属批次权利不受削减；
- 结算执行分两阶段（先整体校验试算，再统一入账），配合幂等键与锁，
  并发结算不会重复扣股或重复付款。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

from .approvals import BatchStatus, Decision, FieldDiff, Role, SettlementBatch
from .errors import (
    AmendmentError,
    ConflictError,
    MissingMarketDataError,
    PlatformError,
    WorkflowError,
)
from .ledger import Ledger, LedgerEntry
from .models import (
    DEFAULT_TERMINATION_POLICIES,
    AgreementTerms,
    BlackoutWindow,
    FxRate,
    Grant,
    LeavePeriod,
    MarketPrice,
    Receipt,
    ServicePeriod,
    SettlementMethod,
    SettlementRecord,
    TaxIdentity,
    Termination,
    TerminationPolicy,
    TerminationType,
    Tranche,
    TrancheSpec,
    TrancheStatus,
)
from .settlement import SettlementQuote, compute_settlement
from .timeutil import expired, local_date, reached
from .vesting import compute_vest_date

SHARE = "SHARE"


class EquityPlatform:
    def __init__(self) -> None:
        self.employees: dict[str, str] = {}
        self.grants: dict[str, Grant] = {}
        self.tranches: dict[str, Tranche] = {}
        self.leaves: dict[str, list[LeavePeriod]] = {}
        self.service_periods: dict[str, list[ServicePeriod]] = {}
        self.tax_identities: dict[str, TaxIdentity] = {}
        self.performance_actuals: dict[tuple[str, int], Decimal] = {}
        self.prices: dict[tuple[str, date], MarketPrice] = {}
        self.fx_rates: dict[tuple[str, str, date], FxRate] = {}
        self.blackouts: list[BlackoutWindow] = []
        self.terminations: dict[str, Termination] = {}
        self.batches: dict[str, SettlementBatch] = {}
        self.batch_results: dict[str, tuple[SettlementRecord, ...]] = {}
        self.settlements: dict[str, SettlementRecord] = {}
        self.receipts: list[Receipt] = []
        self.ledger = Ledger()
        self._seq = 0
        self._exec_lock = threading.Lock()

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    # ------------------------------------------------------------------ 基础资料

    def register_employee(self, employee_id: str, name: str) -> None:
        if employee_id in self.employees:
            raise ConflictError(f"员工 {employee_id} 已登记")
        self.employees[employee_id] = name

    def create_grant(
        self,
        grant_id: str,
        employee_id: str,
        symbol: str,
        *,
        grant_date: date,
        cutoff_timezone: str,
        tranches: list[TrancheSpec],
        settlement_method: SettlementMethod,
        post_termination_window_days: int = 90,
        effective_from: date | None = None,
    ) -> Grant:
        if employee_id not in self.employees:
            raise PlatformError(f"员工 {employee_id} 未登记")
        if grant_id in self.grants:
            raise ConflictError(f"授予 {grant_id} 已存在")
        terms = AgreementTerms(
            version=1,
            effective_from=effective_from or grant_date,
            grant_date=grant_date,
            cutoff_timezone=cutoff_timezone,
            tranches=tuple(tranches),
            settlement_method=SettlementMethod(settlement_method),
            post_termination_window_days=post_termination_window_days,
        )
        grant = Grant(grant_id, employee_id, symbol, [terms])
        self.grants[grant_id] = grant
        for spec in terms.tranches:
            self._create_tranche(grant, terms, spec)
        return grant

    def _create_tranche(self, grant: Grant, terms: AgreementTerms, spec: TrancheSpec) -> Tranche:
        tranche = Tranche(
            tranche_id=f"{grant.grant_id}:{spec.sequence}",
            grant_id=grant.grant_id,
            employee_id=grant.employee_id,
            sequence=spec.sequence,
            spec_shares=spec.shares,
            required_service_days=spec.required_service_days,
            cutoff_timezone=terms.cutoff_timezone,
            terms_version=terms.version,
            performance=spec.performance,
        )
        self.tranches[tranche.tranche_id] = tranche
        return tranche

    # ------------------------------------------------------------------ 协议修订（只向后形成版本）

    def amend_grant(
        self,
        grant_id: str,
        *,
        effective_from: date,
        at: datetime,
        tranches: list[TrancheSpec] | None = None,
        settlement_method: SettlementMethod | None = None,
        post_termination_window_days: int | None = None,
    ) -> AgreementTerms:
        grant = self._grant(grant_id)
        current = grant.terms_history[-1]
        today = local_date(current.cutoff_timezone, at)
        if effective_from < today:
            raise AmendmentError("协议修订只能向后生效，不允许追溯")

        new_specs = tuple(tranches) if tranches is not None else current.tranches
        by_seq = {s.sequence: s for s in new_specs}
        if len(by_seq) != len(new_specs):
            raise AmendmentError("批次序号重复")

        for spec in current.tranches:
            tranche = self.tranches[f"{grant_id}:{spec.sequence}"]
            new_spec = by_seq.get(spec.sequence)
            if tranche.status is TrancheStatus.SETTLED:
                if (
                    new_spec is None
                    or new_spec.shares != spec.shares
                    or new_spec.required_service_days != spec.required_service_days
                ):
                    raise AmendmentError(f"批次 {tranche.tranche_id} 已结算，不得倒改")
            elif tranche.status in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE):
                if new_spec is None:
                    raise AmendmentError(f"批次 {tranche.tranche_id} 已归属，不得删除")
                if (
                    new_spec.shares < spec.shares
                    or new_spec.required_service_days > spec.required_service_days
                ):
                    raise AmendmentError(f"批次 {tranche.tranche_id} 已归属，不得削减其权利")

        terms = AgreementTerms(
            version=current.version + 1,
            effective_from=effective_from,
            grant_date=current.grant_date,
            cutoff_timezone=current.cutoff_timezone,
            tranches=new_specs,
            settlement_method=(
                SettlementMethod(settlement_method)
                if settlement_method is not None
                else current.settlement_method
            ),
            post_termination_window_days=(
                post_termination_window_days
                if post_termination_window_days is not None
                else current.post_termination_window_days
            ),
        )
        grant.terms_history.append(terms)

        for spec in terms.tranches:
            tranche = self.tranches.get(f"{grant_id}:{spec.sequence}")
            if tranche is None:
                self._create_tranche(grant, terms, spec)
            elif tranche.status is TrancheStatus.PENDING:
                tranche.spec_shares = spec.shares
                tranche.required_service_days = spec.required_service_days
                tranche.performance = spec.performance
                tranche.terms_version = terms.version
        for spec in current.tranches:
            if spec.sequence not in by_seq:
                tranche = self.tranches[f"{grant_id}:{spec.sequence}"]
                if tranche.status is TrancheStatus.PENDING:
                    tranche.transition(
                        TrancheStatus.FORFEITED, at, f"协议版本 v{terms.version} 移除该批次"
                    )
        return terms

    # ------------------------------------------------------------------ 资料登记

    def add_service_period(self, employee_id: str, start: date, end: date) -> ServicePeriod:
        period = ServicePeriod(employee_id, self._range(start, end))
        self.service_periods.setdefault(employee_id, []).append(period)
        return period

    def add_leave(
        self, employee_id: str, leave_id: str, start: date, end: date, suspends_vesting: bool = True
    ) -> LeavePeriod:
        leave = LeavePeriod(leave_id, employee_id, self._range(start, end), suspends_vesting)
        self.leaves.setdefault(employee_id, []).append(leave)
        return leave

    def record_performance_actual(self, grant_id: str, sequence: int, actual: Decimal) -> None:
        tranche = self._tranche(f"{grant_id}:{sequence}")
        if tranche.performance is None:
            raise PlatformError(f"批次 {tranche.tranche_id} 无绩效条件，不能登记绩效")
        self.performance_actuals[(grant_id, sequence)] = actual

    def set_tax_identity(
        self, employee_id: str, tax_residency: str, withholding_rate: Decimal, tax_currency: str
    ) -> TaxIdentity:
        identity = TaxIdentity(employee_id, tax_residency, withholding_rate, tax_currency)
        self.tax_identities[employee_id] = identity
        return identity

    def add_market_price(self, symbol: str, price_date: date, currency: str, price: Decimal) -> MarketPrice:
        record = MarketPrice(symbol, price_date, currency, price)
        self.prices[(symbol, price_date)] = record
        return record

    def add_fx_rate(self, base: str, quote: str, rate_date: date, rate: Decimal) -> FxRate:
        record = FxRate(base, quote, rate_date, rate)
        self.fx_rates[(base, quote, rate_date)] = record
        return record

    def add_blackout(self, window_id: str, start: date, end: date, reason: str = "") -> BlackoutWindow:
        window = BlackoutWindow(window_id, self._range(start, end), reason)
        self.blackouts.append(window)
        return window

    # ------------------------------------------------------------------ 归属重算

    def recalculate(self, grant_id: str, as_of: datetime) -> list[Tranche]:
        """按当前生效版本与最新资料重算归属。

        已结算与已失效批次不再变动；已归属批次只随黑窗期在锁定/可执行间切换；
        待满足批次按最新休假与绩效资料重新推算。
        """
        grant = self._grant(grant_id)
        day = local_date(grant.terms_history[-1].cutoff_timezone, as_of)
        terms = grant.terms_as_of(day)
        leaves = self.leaves.get(grant.employee_id, [])
        changed: list[Tranche] = []
        for spec in terms.tranches:
            tranche = self.tranches.get(f"{grant_id}:{spec.sequence}")
            if tranche is None:
                tranche = self._create_tranche(grant, terms, spec)
            if tranche.status in (TrancheStatus.SETTLED, TrancheStatus.FORFEITED):
                continue
            if tranche.status in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE):
                if self._refresh_release_state(tranche, day, as_of):
                    changed.append(tranche)
                continue
            # 待满足：重算归属日并评估条件
            tranche.vest_date = compute_vest_date(
                terms.grant_date, tranche.required_service_days, leaves
            )
            if not reached(tranche.vest_date, tranche.cutoff_timezone, as_of):
                continue
            factor = self._performance_factor(tranche)
            if factor is None:
                continue  # 绩效未评定，继续等待
            tranche.performance_factor = factor
            tranche.vested_shares = tranche.spec_shares * factor
            tranche.forfeited_shares = tranche.spec_shares - tranche.vested_shares
            if tranche.vested_shares <= 0:
                tranche.transition(TrancheStatus.FORFEITED, as_of, "绩效未达标，批次失效")
            else:
                target = TrancheStatus.LOCKED if self._in_blackout(day) else TrancheStatus.RELEASABLE
                tranche.transition(target, as_of, "服务与绩效条件已满足")
            changed.append(tranche)
        return changed

    def recalculate_employee(self, employee_id: str, as_of: datetime) -> list[Tranche]:
        changed: list[Tranche] = []
        for grant in self._grants_of(employee_id):
            changed.extend(self.recalculate(grant.grant_id, as_of))
        return changed

    def _refresh_release_state(self, tranche: Tranche, day: date, as_of: datetime) -> bool:
        target = TrancheStatus.LOCKED if self._in_blackout(day) else TrancheStatus.RELEASABLE
        if tranche.status is target:
            return False
        reason = "黑窗期锁定" if target is TrancheStatus.LOCKED else "黑窗期结束，解除锁定"
        tranche.transition(target, as_of, reason)
        return True

    def _performance_factor(self, tranche: Tranche) -> Decimal | None:
        if tranche.performance is None:
            return Decimal("1")
        actual = self.performance_actuals.get((tranche.grant_id, tranche.sequence))
        if actual is None:
            return None
        return tranche.performance.factor(actual)

    def _in_blackout(self, day: date) -> bool:
        return any(w.period.contains(day) for w in self.blackouts)

    # ------------------------------------------------------------------ 离职

    def terminate(
        self,
        employee_id: str,
        termination_type: TerminationType,
        termination_date: date,
        *,
        at: datetime,
        policy: TerminationPolicy | None = None,
    ) -> Termination:
        ttype = TerminationType(termination_type)
        policy = policy or DEFAULT_TERMINATION_POLICIES[ttype]
        termination = Termination(employee_id, ttype, termination_date, policy, at)
        self.terminations[employee_id] = termination
        for grant in self._grants_of(employee_id):
            for tranche in self._tranches_of(grant.grant_id):
                if tranche.status in (TrancheStatus.SETTLED, TrancheStatus.FORFEITED):
                    continue
                if tranche.status is TrancheStatus.PENDING:
                    if policy.accelerate_unvested:
                        self._accelerate(tranche, termination_date, at)
                    elif policy.forfeit_unvested:
                        tranche.transition(
                            TrancheStatus.FORFEITED, at, f"离职（{ttype.value}）：未归属批次失效"
                        )
            for tranche in self._tranches_of(grant.grant_id):
                if tranche.status not in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE):
                    continue
                if policy.forfeit_releasable:
                    tranche.transition(
                        TrancheStatus.FORFEITED, at, f"离职（{ttype.value}）：未结算批次失效"
                    )
                else:
                    tranche.settlement_deadline = termination_date + timedelta(
                        days=policy.window_days
                    )
        return termination

    def _accelerate(self, tranche: Tranche, vest_day: date, at: datetime) -> None:
        factor = Decimal("1")
        if tranche.performance is not None:
            actual = self.performance_actuals.get((tranche.grant_id, tranche.sequence))
            if actual is None:
                tranche.transition(
                    TrancheStatus.FORFEITED, at, "绩效未评定，无法加速归属，批次失效"
                )
                return
            factor = tranche.performance.factor(actual)
        tranche.performance_factor = factor
        tranche.vested_shares = tranche.spec_shares * factor
        tranche.forfeited_shares = tranche.spec_shares - tranche.vested_shares
        tranche.vest_date = vest_day
        if tranche.vested_shares <= 0:
            tranche.transition(TrancheStatus.FORFEITED, at, "加速归属后无可归属股数")
        else:
            tranche.transition(TrancheStatus.RELEASABLE, at, "离职加速归属")

    def expire_windows(self, as_of: datetime) -> list[Tranche]:
        """离职后结算窗口届满仍未结算的批次转为失效。"""
        expired_tranches = []
        for tranche in self.tranches.values():
            if (
                tranche.settlement_deadline is not None
                and tranche.status in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE)
                and expired(tranche.settlement_deadline, tranche.cutoff_timezone, as_of)
            ):
                tranche.transition(
                    TrancheStatus.FORFEITED, as_of, "离职后结算窗口届满，批次失效"
                )
                expired_tranches.append(tranche)
        return expired_tranches

    # ------------------------------------------------------------------ 结算批次与三方复核

    def create_batch(self, batch_id: str, tranche_ids: list[str], *, at: datetime) -> SettlementBatch:
        if batch_id in self.batches:
            raise ConflictError(f"结算批次 {batch_id} 已存在")
        if not tranche_ids:
            raise WorkflowError("结算批次不能为空")
        if len(set(tranche_ids)) != len(tranche_ids):
            raise WorkflowError("结算批次内存在重复归属批次")
        for tid in tranche_ids:
            tranche = self._tranche(tid)
            if tranche.status in (TrancheStatus.SETTLED, TrancheStatus.FORFEITED):
                raise WorkflowError(f"批次 {tid} 已终结，不能加入结算批次")
        batch = SettlementBatch(batch_id, tuple(tranche_ids), at)
        self.batches[batch_id] = batch
        return batch

    def submit_batch(self, batch_id: str) -> SettlementBatch:
        batch = self._batch(batch_id)
        batch.submit()
        return batch

    def resubmit_batch(self, batch_id: str) -> SettlementBatch:
        batch = self._batch(batch_id)
        batch.resubmit()
        return batch

    def review_batch(
        self,
        batch_id: str,
        *,
        role: Role,
        decision: Decision,
        comment: str = "",
        differences: list[FieldDiff] = (),
        at: datetime,
    ) -> SettlementBatch:
        batch = self._batch(batch_id)
        batch.record_review(
            role=Role(role),
            decision=Decision(decision),
            comment=comment,
            differences=tuple(differences),
            at=at,
        )
        return batch

    # ------------------------------------------------------------------ 结算执行（并发安全）

    def execute_batch(
        self, batch_id: str, *, idempotency_key: str, as_of: datetime
    ) -> tuple[SettlementRecord, ...]:
        with self._exec_lock:
            batch = self._batch(batch_id)
            if batch.status is BatchStatus.EXECUTED:
                if batch.idempotency_key == idempotency_key:
                    return self.batch_results[batch_id]
                raise ConflictError(f"结算批次 {batch_id} 已执行，幂等键不一致，拒绝重复结算")
            if batch.status is not BatchStatus.APPROVED:
                raise WorkflowError(f"结算批次 {batch_id} 未获三方确认，不能执行")

            # 第一阶段：整体校验与试算，任何失败都不入账
            plan: list[tuple[Tranche, SettlementQuote]] = []
            for tid in batch.tranche_ids:
                tranche = self.tranches[tid]
                if tranche.status is TrancheStatus.SETTLED:
                    raise ConflictError(f"批次 {tid} 已结算，禁止重复扣股或付款")
                if tranche.status is TrancheStatus.FORFEITED:
                    raise WorkflowError(f"批次 {tid} 已失效，不能结算")
                day = local_date(tranche.cutoff_timezone, as_of)
                if tranche.status is TrancheStatus.LOCKED and self._in_blackout(day):
                    raise WorkflowError(f"批次 {tid} 处于黑窗期，结算顺延至黑窗结束")
                if tranche.status not in (TrancheStatus.LOCKED, TrancheStatus.RELEASABLE):
                    raise WorkflowError(f"批次 {tid} 状态为 {tranche.status.value}，不能结算")
                plan.append((tranche, self._build_quote(tranche, as_of)))

            # 第二阶段：统一入账并封存结果
            records = []
            for tranche, quote in plan:
                key = f"{idempotency_key}:{tranche.tranche_id}"
                entries = self._ledger_entries(tranche, quote, key, as_of)
                posted = self.ledger.post(key, entries)
                record = SettlementRecord(
                    settlement_id=self._next_id("stl"),
                    tranche_id=tranche.tranche_id,
                    grant_id=tranche.grant_id,
                    employee_id=tranche.employee_id,
                    terms_version=tranche.terms_version,
                    method=quote.method,
                    quote=quote,
                    price_date=local_date(tranche.cutoff_timezone, as_of),
                    settled_at=as_of,
                    idempotency_key=key,
                    ledger_entry_ids=tuple(e.entry_id for e in posted),
                )
                if tranche.status is TrancheStatus.LOCKED:
                    tranche.transition(TrancheStatus.RELEASABLE, as_of, "黑窗期结束，解除锁定")
                self.settlements[tranche.tranche_id] = record
                tranche.settlement = record
                tranche.transition(TrancheStatus.SETTLED, as_of, "结算完成")
                records.append(record)

            result = tuple(records)
            batch.status = BatchStatus.EXECUTED
            batch.idempotency_key = idempotency_key
            self.batch_results[batch_id] = result
            return result

    def _build_quote(self, tranche: Tranche, as_of: datetime) -> SettlementQuote:
        grant = self._grant(tranche.grant_id)
        day = local_date(tranche.cutoff_timezone, as_of)
        terms = grant.terms_as_of(day)
        price = self.prices.get((grant.symbol, day))
        if price is None:
            raise MissingMarketDataError(f"缺少 {grant.symbol} 在 {day.isoformat()} 的市场价格")
        tax = self.tax_identities.get(grant.employee_id)
        if tax is None:
            raise MissingMarketDataError(f"员工 {grant.employee_id} 缺少税务身份")
        fx_rate = None
        if tax.tax_currency != price.currency:
            fx = self.fx_rates.get((price.currency, tax.tax_currency, day))
            if fx is None:
                raise MissingMarketDataError(
                    f"缺少 {price.currency}->{tax.tax_currency} 在 {day.isoformat()} 的汇率"
                )
            fx_rate = fx.rate
        return compute_settlement(
            gross_shares=tranche.vested_shares,
            price=price.price,
            price_currency=price.currency,
            tax_currency=tax.tax_currency,
            fx_rate=fx_rate,
            withholding_rate=tax.withholding_rate,
            method=terms.settlement_method,
        )

    def _ledger_entries(
        self, tranche: Tranche, quote: SettlementQuote, key: str, at: datetime
    ) -> list[LedgerEntry]:
        tax = self.tax_identities[tranche.employee_id]
        jurisdiction = tax.tax_residency
        entries = [
            LedgerEntry(self._next_id("led"), key, f"grant_pool:{tranche.grant_id}", SHARE,
                        -quote.gross_shares, "结算扣减授予池", at),
            LedgerEntry(self._next_id("led"), key, f"employee:{tranche.employee_id}:shares", SHARE,
                        Decimal(quote.delivered_shares), "净股到账", at),
        ]
        if quote.shares_for_tax:
            memo = "公司代扣股份缴税" if quote.method is SettlementMethod.NET_SHARE else "卖出股份缴税"
            entries.append(LedgerEntry(self._next_id("led"), key, f"tax_shares:{jurisdiction}",
                                       SHARE, Decimal(quote.shares_for_tax), memo, at))
        if quote.cash_in_lieu > 0:
            entries.append(LedgerEntry(self._next_id("led"), key, f"employee:{tranche.employee_id}:cash",
                                       quote.price_currency, quote.cash_in_lieu, "不足一股现金补偿", at))
        if quote.cash_residual_to_employee > 0:
            entries.append(LedgerEntry(self._next_id("led"), key, f"employee:{tranche.employee_id}:cash",
                                       quote.price_currency, quote.cash_residual_to_employee,
                                       "多抵税款返还", at))
        if quote.tax_due_tax_ccy > 0:
            entries.append(LedgerEntry(self._next_id("led"), key, f"tax_authority:{jurisdiction}",
                                       quote.tax_currency, quote.tax_due_tax_ccy, "代缴税款", at))
        if quote.cash_tax_due_from_employee > 0:
            entries.append(LedgerEntry(self._next_id("led"), key, f"employee:{tranche.employee_id}:receivable",
                                       quote.price_currency, quote.cash_tax_due_from_employee,
                                       "员工应补缴税款", at))
        return entries

    # ------------------------------------------------------------------ 回执

    def record_receipt(
        self, tranche_id: str, channel: str, ok: bool, detail: str = "", *, at: datetime
    ) -> Receipt:
        if tranche_id not in self.settlements:
            raise WorkflowError(f"批次 {tranche_id} 尚未结算，不能登记回执")
        receipt = Receipt(tranche_id, channel, bool(ok), detail, at)
        self.receipts.append(receipt)
        return receipt

    # ------------------------------------------------------------------ 查询辅助

    def _grant(self, grant_id: str) -> Grant:
        try:
            return self.grants[grant_id]
        except KeyError:
            raise PlatformError(f"授予 {grant_id} 不存在") from None

    def _tranche(self, tranche_id: str) -> Tranche:
        try:
            return self.tranches[tranche_id]
        except KeyError:
            raise PlatformError(f"批次 {tranche_id} 不存在") from None

    def _batch(self, batch_id: str) -> SettlementBatch:
        try:
            return self.batches[batch_id]
        except KeyError:
            raise PlatformError(f"结算批次 {batch_id} 不存在") from None

    def _grants_of(self, employee_id: str) -> list[Grant]:
        return [g for g in self.grants.values() if g.employee_id == employee_id]

    def _tranches_of(self, grant_id: str) -> list[Tranche]:
        return sorted(
            (t for t in self.tranches.values() if t.grant_id == grant_id),
            key=lambda t: t.sequence,
        )

    @staticmethod
    def _range(start: date, end: date):
        from .models import DateRange

        return DateRange(start, end)
