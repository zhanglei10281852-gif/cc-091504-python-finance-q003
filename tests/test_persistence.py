from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from helpers import GRANT, at, make_grant, make_service, tranche

from equity.enums import TrancheState
from equity.service import EquityService


class PersistenceTest(unittest.TestCase):
    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = make_service(runtime_dir=tmp)
            make_grant(service)
            service.evaluate_grant(GRANT, now=at(2024, 1, 2))
            helpers.confirm_all(service, "T1", when=at(2024, 1, 3))
            receipt = service.settle("T1", idempotency_key="K1", now=at(2024, 1, 4))

            self.assertTrue((Path(tmp) / "state.json").exists())
            self.assertTrue((Path(tmp) / "events.jsonl").exists())

            # 模拟重启:新实例从 .runtime 恢复
            restored = EquityService(runtime_dir=tmp)
            self.assertEqual(TrancheState.SETTLED, tranche(restored, "T1").state)
            # 幂等键仍然有效,不会重复扣股
            replay = restored.settle("T1", idempotency_key="K1", now=at(2024, 1, 5))
            self.assertEqual(receipt.receipt_id, replay.receipt_id)
            self.assertEqual(Decimal("1000"), restored.shares_debited("T1"))
            # 事件日志连续
            self.assertTrue(len(restored.store.state.events) > 0)


if __name__ == "__main__":
    unittest.main()
