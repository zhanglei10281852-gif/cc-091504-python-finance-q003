from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import helpers  # noqa: F401  (设置 src 路径)
from app import create_server
from equity.service import EquityService


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = EquityService()
        cls.server = create_server("127.0.0.1", 0, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path: str):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, _ = self._get("/health")
        self.assertEqual(200, status)

        status, _ = self._post("/api/tax-profiles", {
            "employee_id": "E2001", "jurisdiction": "CN", "residency": "RESIDENT",
            "tax_currency": "CNY",
            "brackets": [{"lower": "0", "rate": "0.10"}],
        })
        self.assertEqual(200, status)

        self._post("/api/market/prices", {"at": "2024-01-01T00:00:00+00:00", "currency": "USD", "price": "40"})
        self._post("/api/market/fx", {"at": "2024-01-01T00:00:00+00:00", "base": "USD", "quote": "CNY", "rate": "7.2"})

        status, grant = self._post("/api/grants", {
            "grant_id": "GR-API", "employee_id": "E2001", "award_type": "RSU",
            "stock_currency": "USD", "service_start": "2023-01-01",
            "effective_from": "2023-01-01", "settlement_mode": "net_share",
            "tranches": [
                {"tranche_id": "A1", "sequence": 1, "scheduled_shares": "100",
                 "base_vest_date": "2024-01-01"},
            ],
            "now": "2023-01-01T00:00:00+00:00",
        })
        self.assertEqual(201, status)
        self.assertEqual("PENDING", grant["tranches"][0]["state"])

        status, evaluated = self._post("/api/grants/GR-API/evaluate", {"now": "2024-01-02T00:00:00+00:00"})
        self.assertEqual(200, status)
        self.assertEqual("LOCKED", evaluated["transitions"][0]["to"])

        for role in ("HR", "PAYROLL", "SECURITIES"):
            status, _ = self._post("/api/tranches/A1/reviews", {
                "role": role, "decision": "CONFIRMED", "opinion": "ok",
                "now": "2024-01-03T00:00:00+00:00",
            })
            self.assertEqual(200, status)

        status, receipt = self._post("/api/tranches/A1/settle", {
            "idempotency_key": "API-K1", "now": "2024-01-04T00:00:00+00:00",
        })
        self.assertEqual(200, status)
        self.assertEqual("COMPLETED", receipt["status"])
        # 幂等重放
        status, replay = self._post("/api/tranches/A1/settle", {
            "idempotency_key": "API-K1", "now": "2024-01-05T00:00:00+00:00",
        })
        self.assertEqual(receipt["receipt_id"], replay["receipt_id"])

        status, statement = self._get("/api/employees/E2001/statement")
        self.assertEqual(200, status)
        tranche_view = statement["grants"][0]["tranches"][0]
        self.assertEqual("SETTLED", tranche_view["状态"])
        self.assertIsNotNone(tranche_view["结算"]["构成"])

        status, dashboard = self._get("/api/admin/dashboard")
        self.assertEqual(200, status)
        self.assertIn("待完成审批", dashboard)

    def test_unknown_route_404(self):
        status, _ = self._get("/api/nope")
        self.assertEqual(404, status)

    def test_validation_error_400(self):
        status, body = self._post("/api/grants", {"grant_id": "X"})
        self.assertEqual(400, status)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
