"""分类账：幂等键 + 锁保证并发结算不会重复扣股或重复付款。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    idempotency_key: str
    account: str
    currency: str  # SHARE 或 ISO 货币代码
    amount: Decimal
    memo: str
    created_at: datetime


class Ledger:
    """只增不改的入账簿。同一幂等键重复提交返回原分录，绝不重复入账。"""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._by_key: dict[str, tuple[LedgerEntry, ...]] = {}
        self._lock = threading.Lock()

    def post(self, idempotency_key: str, entries: list[LedgerEntry]) -> tuple[LedgerEntry, ...]:
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                return existing
            posted = tuple(entries)
            self._entries.extend(posted)
            self._by_key[idempotency_key] = posted
            return posted

    def has_key(self, idempotency_key: str) -> bool:
        with self._lock:
            return idempotency_key in self._by_key

    def balance(self, account: str, currency: str) -> Decimal:
        with self._lock:
            return sum(
                (e.amount for e in self._entries if e.account == account and e.currency == currency),
                Decimal("0"),
            )

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)
