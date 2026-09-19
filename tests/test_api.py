from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
from equity_platform import EquityPlatform, TrancheStatus  # noqa: E402

AS_OF = "2025-01-14T02:00:00+00:00"

GRANT_PAYLOAD = {
    "grant_id": "G1",
    "employee_id": "E1",
    "symbol": "ACME",
    "grant_date": "2024-01-15",
    "cutoff_timezone": "Asia/Shanghai",
    "settlement_method": "net_share",
    "tranches": [
        {"sequence": 1, "required_service_days": 365, "shares": "100"},
        {"sequence": 2, "required_service_days": 730, "shares": "100"},
    ],
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        runtime = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(app, "RUNTIME_DIR", runtime),
            mock.patch.object(app, "COMMAND_LOG", runtime / "commands.jsonl"),
        ]
        for patch in self.patches:
            patch.start()
        self.platform = EquityPlatform()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def call(self, method, path, payload=None, query=None):
        return app.handle_request(self.platform, method, path, payload, query)

    def build_and_settle(self):
        self.call("POST", "/employees", {"employee_id": "E1", "name": "跨境员工"})
        self.call("POST", "/grants", GRANT_PAYLOAD)
        self.call("POST", "/employees/E1/tax-identity",
                  {"tax_residency": "CN", "withholding_rate": "0.35", "tax_currency": "USD"})
        self.call("POST", "/market-prices",
                  {"symbol": "ACME", "date": "2025-01-14", "currency": "USD", "price": "50"})
        self.call("POST", "/grants/G1/recalculate", {"as_of": AS_OF})
        self.call("POST", "/batches", {"batch_id": "B1", "tranche_ids": ["G1:1"], "at": AS_OF})
        self.call("POST", "/batches/B1/submit", {})
        for role in ("HR", "PAYROLL", "STOCK_ADMIN"):
            self.call("POST", "/batches/B1/reviews",
                      {"role": role, "decision": "APPROVED", "at": AS_OF})
        return self.call("POST", "/batches/B1/execute",
                         {"idempotency_key": "k1", "as_of": AS_OF})

    def test_full_settlement_flow_over_api(self):
        status, body = self.build_and_settle()
        self.assertEqual(200, status)
        self.assertEqual(["G1:1"], body["settled"])

        status, statement = self.call(
            "GET", "/employees/E1/statement", query={"as_of": [AS_OF]}
        )
        self.assertEqual(200, status)
        first = statement["tranches"][0]
        self.assertEqual("SETTLED", first["status"])
        self.assertEqual(65, first["settlement"]["delivered_shares"])
        self.assertEqual("1750.00", first["settlement"]["tax_due_tax_ccy"])

        status, tranche = self.call("GET", "/tranches/G1:1")
        self.assertEqual("SETTLED", tranche["status"])

    def test_error_mapping(self):
        status, body = self.call("GET", "/nope")
        self.assertEqual(404, status)
        self.call("POST", "/employees", {"employee_id": "E1"})
        self.call("POST", "/grants", GRANT_PAYLOAD)
        # 未三方确认即执行 → 409
        self.call("POST", "/batches", {"batch_id": "B1", "tranche_ids": ["G1:1"], "at": AS_OF})
        self.call("POST", "/batches/B1/submit", {})
        status, body = self.call("POST", "/batches/B1/execute",
                                 {"idempotency_key": "k1", "as_of": AS_OF})
        self.assertEqual(409, status)
        self.assertEqual("WorkflowError", body["type"])
        # 缺少必填字段 → 400
        status, _ = self.call("POST", "/employees", {})
        self.assertEqual(400, status)

    def test_command_log_replay_restores_state(self):
        self.build_and_settle()
        restored = EquityPlatform()
        replayed = app.replay_commands(restored)
        self.assertGreater(replayed, 0)
        self.assertEqual(TrancheStatus.SETTLED, restored.tranches["G1:1"].status)
        self.assertEqual(
            self.platform.ledger.balance("employee:E1:shares", "SHARE"),
            restored.ledger.balance("employee:E1:shares", "SHARE"),
        )


if __name__ == "__main__":
    unittest.main()
