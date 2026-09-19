"""HTTP 层：JSON API + 命令日志持久化。

- 所有接口接收/返回 JSON；日期用 ISO 字符串，金额/数量用字符串或数字；
- 每个成功的变更请求按顺序追加到 .runtime/commands.jsonl，重启后自动重放，
  保证服务状态可恢复且口径一致；
- 业务拒绝映射为 409，参数错误为 400，未知资源为 404。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from equity_platform import (
    EquityPlatform,
    FieldDiff,
    PerformanceCondition,
    TrancheSpec,
)
from equity_platform.errors import PlatformError
from equity_platform.reports import (
    employee_statement,
    failed_receipts,
    pending_approvals,
    post_termination_tranches,
)

SERVICE_NAME = '股权激励管理服务'

RUNTIME_DIR = Path(".runtime")
COMMAND_LOG = RUNTIME_DIR / "commands.jsonl"

_POST_LOCK = threading.Lock()


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ------------------------------------------------------------------ 序列化

def to_jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return sorted(to_jsonable(v) for v in obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


# ------------------------------------------------------------------ 入参解析

def _date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _dt(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value)
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            # 查询串中未编码的 '+' 会被解码为空格，还原为时区偏移
            head, _, offset = text.rpartition(" ")
            moment = datetime.fromisoformat(f"{head}+{offset}")
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _spec(payload: dict) -> TrancheSpec:
    perf = payload.get("performance")
    condition = None
    if perf:
        condition = PerformanceCondition(
            metric=perf["metric"],
            target=_dec(perf["target"]),
            threshold=_dec(perf.get("threshold", "0")),
            cap=_dec(perf.get("cap", "1")),
        )
    return TrancheSpec(
        sequence=int(payload["sequence"]),
        required_service_days=int(payload["required_service_days"]),
        shares=_dec(payload["shares"]),
        performance=condition,
    )


def _specs(payload) -> list[TrancheSpec] | None:
    if payload is None:
        return None
    return [_spec(item) for item in payload]


# ------------------------------------------------------------------ 路由

def _dispatch(platform: EquityPlatform, method: str, path: str, payload: dict, query: dict):
    seg = [s for s in path.split("/") if s]

    if method == "GET" and seg == ["health"]:
        return 200, health_payload()

    if method == "POST" and seg == ["employees"]:
        platform.register_employee(payload["employee_id"], payload.get("name", ""))
        return 201, {"employee_id": payload["employee_id"]}

    if method == "POST" and seg == ["grants"]:
        grant = platform.create_grant(
            payload["grant_id"],
            payload["employee_id"],
            payload["symbol"],
            grant_date=_date(payload["grant_date"]),
            cutoff_timezone=payload["cutoff_timezone"],
            tranches=_specs(payload["tranches"]),
            settlement_method=payload["settlement_method"],
            post_termination_window_days=int(payload.get("post_termination_window_days", 90)),
            effective_from=_date(payload["effective_from"]) if payload.get("effective_from") else None,
        )
        return 201, {"grant_id": grant.grant_id, "terms_version": 1}

    if method == "POST" and len(seg) == 3 and seg[0] == "grants" and seg[2] == "amendments":
        terms = platform.amend_grant(
            seg[1],
            effective_from=_date(payload["effective_from"]),
            at=_dt(payload["at"]),
            tranches=_specs(payload.get("tranches")),
            settlement_method=payload.get("settlement_method"),
            post_termination_window_days=payload.get("post_termination_window_days"),
        )
        return 201, {"grant_id": seg[1], "terms_version": terms.version}

    if method == "POST" and len(seg) == 3 and seg[0] == "grants" and seg[2] == "recalculate":
        changed = platform.recalculate(seg[1], _dt(payload["as_of"]))
        return 200, {"changed": [t.tranche_id for t in changed]}

    if method == "POST" and len(seg) == 3 and seg[0] == "grants" and seg[2] == "performance":
        platform.record_performance_actual(seg[1], int(payload["sequence"]), _dec(payload["actual"]))
        return 200, {"grant_id": seg[1], "sequence": int(payload["sequence"])}

    if method == "POST" and len(seg) == 3 and seg[0] == "employees" and seg[2] == "leaves":
        leave = platform.add_leave(
            seg[1], payload["leave_id"], _date(payload["start"]), _date(payload["end"]),
            bool(payload.get("suspends_vesting", True)),
        )
        return 201, {"leave_id": leave.leave_id}

    if method == "POST" and len(seg) == 3 and seg[0] == "employees" and seg[2] == "service-periods":
        period = platform.add_service_period(seg[1], _date(payload["start"]), _date(payload["end"]))
        return 201, {"employee_id": period.employee_id}

    if method == "POST" and len(seg) == 3 and seg[0] == "employees" and seg[2] == "tax-identity":
        identity = platform.set_tax_identity(
            seg[1], payload["tax_residency"], _dec(payload["withholding_rate"]), payload["tax_currency"]
        )
        return 200, {"employee_id": identity.employee_id}

    if method == "POST" and len(seg) == 3 and seg[0] == "employees" and seg[2] == "terminations":
        termination = platform.terminate(
            seg[1], payload["termination_type"], _date(payload["termination_date"]), at=_dt(payload["at"])
        )
        return 200, {"employee_id": termination.employee_id, "type": termination.termination_type}

    if method == "POST" and seg == ["market-prices"]:
        platform.add_market_price(payload["symbol"], _date(payload["date"]), payload["currency"], _dec(payload["price"]))
        return 201, {"symbol": payload["symbol"], "date": payload["date"]}

    if method == "POST" and seg == ["fx-rates"]:
        platform.add_fx_rate(payload["base"], payload["quote"], _date(payload["date"]), _dec(payload["rate"]))
        return 201, {"base": payload["base"], "quote": payload["quote"], "date": payload["date"]}

    if method == "POST" and seg == ["blackouts"]:
        window = platform.add_blackout(
            payload["window_id"], _date(payload["start"]), _date(payload["end"]), payload.get("reason", "")
        )
        return 201, {"window_id": window.window_id}

    if method == "POST" and seg == ["batches"]:
        batch = platform.create_batch(payload["batch_id"], list(payload["tranche_ids"]), at=_dt(payload["at"]))
        return 201, {"batch_id": batch.batch_id, "status": batch.status}

    if method == "POST" and len(seg) == 3 and seg[0] == "batches" and seg[2] == "submit":
        batch = platform.submit_batch(seg[1])
        return 200, {"batch_id": batch.batch_id, "status": batch.status}

    if method == "POST" and len(seg) == 3 and seg[0] == "batches" and seg[2] == "resubmit":
        batch = platform.resubmit_batch(seg[1])
        return 200, {"batch_id": batch.batch_id, "status": batch.status, "version": batch.version}

    if method == "POST" and len(seg) == 3 and seg[0] == "batches" and seg[2] == "reviews":
        batch = platform.review_batch(
            seg[1],
            role=payload["role"],
            decision=payload["decision"],
            comment=payload.get("comment", ""),
            differences=[
                FieldDiff(d["field"], str(d["recorded"]), str(d["expected"]))
                for d in payload.get("differences", [])
            ],
            at=_dt(payload["at"]),
        )
        return 200, {"batch_id": batch.batch_id, "status": batch.status, "version": batch.version}

    if method == "POST" and len(seg) == 3 and seg[0] == "batches" and seg[2] == "execute":
        records = platform.execute_batch(
            seg[1], idempotency_key=payload["idempotency_key"], as_of=_dt(payload["as_of"])
        )
        return 200, {"batch_id": seg[1], "settled": [r.tranche_id for r in records]}

    if method == "POST" and seg == ["receipts"]:
        receipt = platform.record_receipt(
            payload["tranche_id"], payload["channel"], bool(payload["ok"]),
            payload.get("detail", ""), at=_dt(payload["at"]),
        )
        return 201, {"tranche_id": receipt.tranche_id, "channel": receipt.channel, "ok": receipt.ok}

    if method == "POST" and seg == ["maintenance", "expire-windows"]:
        expired_tranches = platform.expire_windows(_dt(payload["as_of"]))
        return 200, {"expired": [t.tranche_id for t in expired_tranches]}

    if method == "GET" and len(seg) == 3 and seg[0] == "employees" and seg[2] == "statement":
        as_of = _dt(query["as_of"][0]) if "as_of" in query else datetime.now(timezone.utc)
        return 200, employee_statement(platform, seg[1], as_of)

    if method == "GET" and seg == ["admin", "pending-approvals"]:
        return 200, {"pending": pending_approvals(platform)}

    if method == "GET" and seg == ["admin", "failed-receipts"]:
        return 200, {"failed": failed_receipts(platform)}

    if method == "GET" and seg == ["admin", "post-termination"]:
        as_of = _dt(query["as_of"][0]) if "as_of" in query else datetime.now(timezone.utc)
        return 200, {"tranches": post_termination_tranches(platform, as_of)}

    if method == "GET" and len(seg) == 2 and seg[0] == "tranches":
        tranche = platform.tranches.get(seg[1])
        if tranche is None:
            return 404, {"error": f"批次 {seg[1]} 不存在"}
        return 200, tranche

    if method == "GET" and len(seg) == 2 and seg[0] == "batches":
        batch = platform.batches.get(seg[1])
        if batch is None:
            return 404, {"error": f"结算批次 {seg[1]} 不存在"}
        return 200, batch

    if method == "GET" and seg == ["ledger", "entries"]:
        return 200, {"entries": platform.ledger.entries()}

    return 404, {"error": "not found"}


def handle_request(
    platform: EquityPlatform,
    method: str,
    path: str,
    payload: dict | None = None,
    query: dict | None = None,
    *,
    record: bool = True,
):
    """统一入口：路由 + 异常映射 + 命令日志。返回 (status, jsonable_body)。"""
    try:
        status, body = _dispatch(platform, method, path, payload or {}, query or {})
    except PlatformError as exc:
        status, body = 409, {"error": str(exc), "type": type(exc).__name__}
    except KeyError as exc:
        status, body = 400, {"error": f"缺少字段: {exc}"}
    except (ValueError, TypeError) as exc:
        status, body = 400, {"error": str(exc)}
    if record and method == "POST" and 200 <= status < 300:
        _append_command(method, path, payload or {})
    return status, to_jsonable(body)


def _append_command(method: str, path: str, payload: dict) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    with COMMAND_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"method": method, "path": path, "payload": payload}, ensure_ascii=False) + "\n")


def replay_commands(platform: EquityPlatform) -> int:
    """启动时重放命令日志，恢复服务状态。返回重放的命令数。"""
    if not COMMAND_LOG.exists():
        return 0
    count = 0
    for line in COMMAND_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        status, _ = handle_request(
            platform, record["method"], record["path"], record.get("payload"), record=False
        )
        if status >= 300:
            raise RuntimeError(f"命令日志重放失败: {line}")
        count += 1
    return count


# ------------------------------------------------------------------ HTTP 服务

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        payload = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._respond(400, {"error": "请求体不是合法 JSON"})
                    return
        if method == "POST":
            with _POST_LOCK:  # 变更串行化，保证并发请求下口径一致
                status, body = handle_request(self.server.platform, method, parsed.path, payload, query)
        else:
            status, body = handle_request(self.server.platform, method, parsed.path, payload, query)
        self._respond(status, body)

    def _respond(self, status: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, platform: EquityPlatform | None = None) -> ThreadingHTTPServer:
    if platform is None:
        platform = EquityPlatform()
        replay_commands(platform)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.platform = platform
    return server
