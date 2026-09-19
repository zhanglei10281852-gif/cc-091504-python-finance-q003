from __future__ import annotations

import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import TrancheStatus  # noqa: E402
from equity_platform.errors import ConflictError  # noqa: E402
from helpers import D, vested_platform  # noqa: E402


def approved_batch(platform, as_of, batch_id="B1", tranches=("G1:1",)):
    platform.create_batch(batch_id, list(tranches), at=as_of)
    platform.submit_batch(batch_id)
    for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
        platform.review_batch(batch_id, role=role, decision="APPROVED", at=as_of)


class ConcurrentSettlementTest(unittest.TestCase):
    def _run_threads(self, fn, n=8):
        barrier = threading.Barrier(n)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                outcomes.append(("ok", fn()))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("err", exc))

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return outcomes

    def test_same_idempotency_key_settles_exactly_once(self):
        platform, as_of = vested_platform()
        approved_batch(platform, as_of)
        outcomes = self._run_threads(
            lambda: platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        )
        self.assertTrue(all(kind == "ok" for kind, _ in outcomes))
        results = {id(value) for kind, value in outcomes}
        self.assertEqual(1, len(results))  # 所有调用拿到同一个结算结果
        # 授予池只扣减一次，员工只到账一次
        self.assertEqual(D("-100"), platform.ledger.balance("grant_pool:G1", "SHARE"))
        self.assertEqual(D("65"), platform.ledger.balance("employee:E1:shares", "SHARE"))
        self.assertEqual(D("1750.00"), platform.ledger.balance("tax_authority:CN", "USD"))

    def test_different_idempotency_keys_conflict_without_double_posting(self):
        platform, as_of = vested_platform()
        approved_batch(platform, as_of)
        keys = iter(f"key-{i}" for i in range(8))
        outcomes = self._run_threads(
            lambda: platform.execute_batch("B1", idempotency_key=next(keys), as_of=as_of)
        )
        ok = [v for k, v in outcomes if k == "ok"]
        conflicts = [v for k, v in outcomes if k == "err" and isinstance(v, ConflictError)]
        self.assertEqual(1, len(ok))
        self.assertEqual(7, len(conflicts))
        self.assertEqual(D("-100"), platform.ledger.balance("grant_pool:G1", "SHARE"))
        self.assertEqual(D("65"), platform.ledger.balance("employee:E1:shares", "SHARE"))

    def test_overlapping_batches_cannot_double_settle_tranche(self):
        platform, as_of = vested_platform()
        approved_batch(platform, as_of, "B1")
        approved_batch(platform, as_of, "B2")
        platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        with self.assertRaises(ConflictError):
            platform.execute_batch("B2", idempotency_key="k2", as_of=as_of)
        self.assertEqual(D("-100"), platform.ledger.balance("grant_pool:G1", "SHARE"))
        self.assertEqual(TrancheStatus.SETTLED, platform.tranches["G1:1"].status)

    def test_idempotent_replay_returns_original_record(self):
        platform, as_of = vested_platform()
        approved_batch(platform, as_of)
        first = platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        second = platform.execute_batch("B1", idempotency_key="k1", as_of=as_of)
        self.assertIs(first, second)
        self.assertEqual(1, len(platform.settlements))


if __name__ == "__main__":
    unittest.main()
