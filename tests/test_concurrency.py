from __future__ import annotations

import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import GRANT, at, make_grant, make_service, tranche

from equity.enums import ReceiptStatus, TrancheState
from equity.errors import ConflictError
from equity.gateway import DepositoryGateway


def ready_service(**kwargs):
    service = make_service(**kwargs)
    make_grant(service)
    service.evaluate_grant(GRANT, now=at(2024, 1, 2))
    helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
    return service


class ConcurrencyTest(unittest.TestCase):
    def test_same_key_concurrent_settle_settles_once(self):
        service = ready_service()
        results, errors = [], []

        def worker():
            try:
                results.append(service.settle("T1", idempotency_key="K-SAME", now=at(2024, 1, 4)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        self.assertEqual(16, len(results))
        self.assertEqual(1, len({r.receipt_id for r in results}))
        # 股份只扣减一次
        self.assertEqual(Decimal("1000"), service.shares_debited("T1"))
        self.assertEqual(TrancheState.SETTLED, tranche(service, "T1").state)

    def test_different_keys_concurrent_only_one_wins(self):
        service = ready_service()
        receipts, conflicts = [], []

        def worker(i):
            try:
                receipts.append(service.settle("T1", idempotency_key=f"K-{i}", now=at(2024, 1, 4)))
            except ConflictError as exc:
                conflicts.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(1, len(receipts))
        self.assertEqual(7, len(conflicts))
        self.assertTrue(all(c.code == "already_settled" for c in conflicts))
        self.assertEqual(Decimal("1000"), service.shares_debited("T1"))

    def test_failed_receipt_retry_no_double_charge(self):
        gateway = DepositoryGateway(blocked_accounts={helpers.EMPLOYEE})
        service = ready_service(gateway=gateway)
        failed = service.settle("T1", idempotency_key="K-FAIL", now=at(2024, 1, 4))
        self.assertEqual(ReceiptStatus.FAILED, failed.status)
        # 失败不入台账、不改状态
        self.assertEqual(Decimal("0"), service.shares_debited("T1"))
        self.assertEqual(TrancheState.EXERCISABLE, tranche(service, "T1").state)
        # 同键重放返回原失败回执,不重复执行
        replay = service.settle("T1", idempotency_key="K-FAIL", now=at(2024, 1, 5))
        self.assertEqual(failed.receipt_id, replay.receipt_id)
        # 账户恢复后换新键重试
        gateway.unblock_account(helpers.EMPLOYEE)
        retried = service.retry_receipt(failed.receipt_id, new_idempotency_key="K-RETRY", now=at(2024, 1, 6))
        self.assertEqual(ReceiptStatus.COMPLETED, retried.status)
        self.assertEqual(2, retried.attempt)
        self.assertEqual(Decimal("1000"), service.shares_debited("T1"))
        # 失败回执保留可查
        dashboard_receipts = [
            r for r in service.store.state.receipts.values()
        ]
        self.assertEqual(2, len(dashboard_receipts))

    def test_ledger_conservation(self):
        service = ready_service()
        service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))
        record = tranche(service, "T1")
        debited = service.shares_debited("T1")
        # 出库整股数 = 到账 + 扣股抵税
        receipt = service.store.state.receipts[record.receipt_id]
        b = receipt.breakdown
        self.assertEqual(debited, b.net_shares + b.shares_for_tax)
        self.assertEqual(debited, b.whole_shares)


if __name__ == "__main__":
    unittest.main()
