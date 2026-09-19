"""仓储:内存数据结构 + 可重入锁 + 幂等键 + 台账 + 事件日志 + .runtime 持久化。

并发约束:所有变更在 store.lock 内完成,状态转移是"检查-执行"原子操作;
结算以幂等键去重,重复提交返回原回执,不会重复扣股或付款。
持久化:每次变更后快照写入 .runtime/state.json(临时文件原子替换),
事件追加到 .runtime/events.jsonl,重启后可恢复。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import codec
from .codec import register
from .enums import ReceiptStatus
from .models import (
    ApprovalCase,
    BlackoutWindow,
    Grant,
    TaxProfile,
)
from .pricing import FxBook, PriceBook
from .settlement import SettlementBreakdown
from .timeutil import utcnow


@register
@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    idempotency_key: str
    grant_id: str
    tranche_id: str
    employee_id: str
    status: ReceiptStatus
    version: int
    attempt: int
    breakdown: SettlementBreakdown | None
    failure_reason: str | None
    confirmation_no: str | None
    created_at: datetime


@register
@dataclass(frozen=True)
class LedgerEntry:
    """资金/股份台账:只有结算成功才入账,保证并发下不重复扣减。"""

    entry_id: str
    receipt_id: str
    grant_id: str
    tranche_id: str
    kind: str  # SHARE_DEBIT 股份出库 / CASH_DEBIT 现金付出 / CASH_DUE 员工应补
    quantity: Decimal
    currency: str
    at: datetime


@register
@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    kind: str
    payload: dict


@register
@dataclass
class StoreState:
    grants: dict[str, Grant] = field(default_factory=dict)
    tax_profiles: dict[str, TaxProfile] = field(default_factory=dict)
    blackouts: list[BlackoutWindow] = field(default_factory=list)
    approval_cases: dict[str, ApprovalCase] = field(default_factory=dict)
    receipts: dict[str, Receipt] = field(default_factory=dict)
    idempotency: dict[str, str] = field(default_factory=dict)  # key -> receipt_id
    ledger: list[LedgerEntry] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    prices: list = field(default_factory=list)
    fx_rates: list = field(default_factory=list)
    seq: int = 0


class Store:
    def __init__(self, runtime_dir: str | Path | None = None):
        self.lock = threading.RLock()
        self.state = StoreState()
        self.price_book = PriceBook()
        self.fx_book = FxBook()
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        if self.runtime_dir and (self.runtime_dir / "state.json").exists():
            self._load()

    # ---- 事件与持久化 ----

    def emit(self, kind: str, payload: dict) -> Event:
        with self.lock:
            self.state.seq += 1
            event = Event(seq=self.state.seq, at=utcnow(), kind=kind, payload=payload)
            self.state.events.append(event)
            self._persist(event)
            return event

    def _persist(self, event: Event) -> None:
        if not self.runtime_dir:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with open(self.runtime_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(codec.dumps(codec.to_jsonable(event)) + "\n")
        snapshot = codec.dumps(codec.to_jsonable(self.state))
        tmp = self.runtime_dir / "state.json.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(snapshot)
        os.replace(tmp, self.runtime_dir / "state.json")

    def _load(self) -> None:
        text = (self.runtime_dir / "state.json").read_text(encoding="utf-8")
        loaded = codec.loads(text)
        self.state = loaded
        for point in self.state.prices:
            self.price_book.add(point)
        for rate in self.state.fx_rates:
            self.fx_book.add(rate)

    # ---- 市场数据 ----

    def add_price(self, point) -> None:
        with self.lock:
            self.price_book.add(point)
            self.state.prices.append(point)

    def add_fx(self, rate) -> None:
        with self.lock:
            self.fx_book.add(rate)
            self.state.fx_rates.append(rate)
