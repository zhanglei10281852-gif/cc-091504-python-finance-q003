"""HTTP 接口层:标准库实现,JSON 请求/响应。

路由薄封装 EquityService;所有金额与股数在 JSON 中以字符串表示,
日期为 ISO 格式,时刻为带时区的 ISO 格式。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from equity.enums import ReviewDecision, Role, SettlementMode, TerminationType
from equity.errors import ServiceError
from equity.models import (
    FxRatePoint,
    LeavePeriod,
    PerformanceAssessment,
    PayoutStep,
    PricePoint,
    TaxBracket,
    TaxProfile,
    Termination,
    TrancheTerms,
)
from equity.models import BlackoutWindow
from equity.reports import admin_dashboard, employee_statement
from equity.service import EquityService
from equity.timeutil import ensure_utc

SERVICE_NAME = "股权激励管理服务"


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ----------------------------------------------------------------------
# 请求体解析
# ----------------------------------------------------------------------


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _dt(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value))


def _tranche_terms(payload: dict) -> TrancheTerms:
    return TrancheTerms(
        tranche_id=payload["tranche_id"],
        sequence=int(payload["sequence"]),
        scheduled_shares=Decimal(str(payload["scheduled_shares"])),
        base_vest_date=_date(payload["base_vest_date"]),
        performance_metric=payload.get("performance_metric"),
        payout_curve=tuple(
            PayoutStep(threshold=Decimal(str(s["threshold"])), ratio=Decimal(str(s["ratio"])))
            for s in payload.get("payout_curve", [])
        ),
    )


def _grant_view(service: EquityService, grant) -> dict:
    return {
        "grant_id": grant.grant_id,
        "employee_id": grant.employee_id,
        "award_type": grant.award_type,
        "stock_currency": grant.stock_currency,
        "service_start": str(grant.service_start),
        "timezone": grant.timezone,
        "versions": [
            {
                "version": v.version,
                "effective_from": str(v.effective_from),
                "settlement_mode": v.settlement_mode.value,
                "note": v.note,
            }
            for v in grant.versions
        ],
        "leaves": [
            {"start": str(l.start), "end": str(l.end), "days": l.days}
            for l in grant.leaves
        ],
        "termination": (
            {"type": grant.termination.type.value, "date": str(grant.termination.date)}
            if grant.termination
            else None
        ),
        "tranches": [
            {
                "tranche_id": r.terms.tranche_id,
                "state": r.state.value,
                "scheduled_shares": str(r.terms.scheduled_shares),
                "base_vest_date": str(r.terms.base_vest_date),
                "adjusted_vest_date": (
                    str(r.adjusted_vest_date) if r.adjusted_vest_date else None
                ),
                "eligible_shares": (
                    str(r.eligible_shares) if r.eligible_shares is not None else None
                ),
                "governing_version": r.governing_version,
                "lapse_reason": r.lapse_reason,
                "receipt_id": r.receipt_id,
            }
            for r in sorted(grant.tranches.values(), key=lambda x: x.terms.sequence)
        ],
    }


def _receipt_view(receipt) -> dict:
    from equity.codec import plain

    return plain(receipt)


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------


def _handle_post(service: EquityService, segments: list[str], body: dict):
    if segments == ["api", "grants"]:
        grant = service.create_grant(
            grant_id=body["grant_id"],
            employee_id=body["employee_id"],
            award_type=body.get("award_type", "RSU"),
            stock_currency=body["stock_currency"],
            service_start=_date(body["service_start"]),
            settlement_mode=SettlementMode(body["settlement_mode"]),
            tranches=[_tranche_terms(t) for t in body["tranches"]],
            effective_from=_date(body["effective_from"]),
            timezone=body.get("timezone", "Asia/Shanghai"),
            note=body.get("note", "初始授予"),
            now=_dt(body["now"]) if body.get("now") else None,
        )
        return 201, _grant_view(service, grant)

    if len(segments) == 4 and segments[:2] == ["api", "grants"]:
        grant_id, action = segments[2], segments[3]
        if action == "amendments":
            version = service.amend_grant(
                grant_id,
                effective_from=_date(body["effective_from"]),
                settlement_mode=SettlementMode(body["settlement_mode"]),
                tranches=[_tranche_terms(t) for t in body["tranches"]],
                note=body.get("note", ""),
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 201, {"version": version.version, "effective_from": str(version.effective_from)}
        if action == "leaves":
            service.record_leave(
                grant_id,
                LeavePeriod(
                    start=_date(body["start"]),
                    end=_date(body["end"]),
                    reason=body.get("reason", ""),
                ),
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 200, {"recorded": True}
        if action == "performance":
            service.assess_performance(
                grant_id,
                PerformanceAssessment(
                    metric=body["metric"],
                    target=Decimal(str(body["target"])),
                    actual=Decimal(str(body["actual"])),
                    assessed_at=_dt(body["assessed_at"]),
                ),
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 200, {"recorded": True}
        if action == "termination":
            service.record_termination(
                grant_id,
                Termination(
                    type=TerminationType(body["type"]),
                    date=_date(body["date"]),
                    exercise_window_days=body.get("exercise_window_days"),
                ),
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 200, {"recorded": True}
        if action == "evaluate":
            transitions = service.evaluate_grant(
                grant_id, now=_dt(body["now"]) if body.get("now") else None
            )
            return 200, {
                "transitions": [
                    {
                        "from": t.from_state.value,
                        "to": t.to_state.value,
                        "reason": t.reason,
                    }
                    for t in transitions
                ]
            }

    if segments == ["api", "tax-profiles"]:
        profile = TaxProfile(
            employee_id=body["employee_id"],
            jurisdiction=body["jurisdiction"],
            residency=body["residency"],
            tax_currency=body["tax_currency"],
            brackets=tuple(
                TaxBracket(lower=Decimal(str(b["lower"])), rate=Decimal(str(b["rate"])))
                for b in body["brackets"]
            ),
        )
        service.set_tax_profile(profile)
        return 200, {"recorded": True}

    if segments == ["api", "market", "prices"]:
        service.add_price(
            PricePoint(at=_dt(body["at"]), currency=body["currency"], price=Decimal(str(body["price"])))
        )
        return 200, {"recorded": True}

    if segments == ["api", "market", "fx"]:
        service.add_fx(
            FxRatePoint(
                at=_dt(body["at"]),
                base=body["base"],
                quote=body["quote"],
                rate=Decimal(str(body["rate"])),
            )
        )
        return 200, {"recorded": True}

    if segments == ["api", "market", "blackouts"]:
        service.add_blackout(
            BlackoutWindow(
                start=_dt(body["start"]), end=_dt(body["end"]), reason=body.get("reason", "")
            )
        )
        return 200, {"recorded": True}

    if len(segments) == 4 and segments[:2] == ["api", "tranches"]:
        tranche_id, action = segments[2], segments[3]
        if action == "reviews":
            record = service.review(
                tranche_id,
                role=Role(body["role"]),
                decision=ReviewDecision(body["decision"]),
                opinion=body.get("opinion", ""),
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 200, {
                "role": record.role.value,
                "decision": record.decision.value,
                "snapshot_hash": record.snapshot_hash,
                "diff_from_previous": record.diff_from_previous,
            }
        if action == "settle":
            receipt = service.settle(
                tranche_id,
                idempotency_key=body["idempotency_key"],
                now=_dt(body["now"]) if body.get("now") else None,
            )
            return 200, _receipt_view(receipt)

    if len(segments) == 4 and segments[:2] == ["api", "receipts"] and segments[3] == "retry":
        receipt = service.retry_receipt(
            segments[2],
            new_idempotency_key=body["idempotency_key"],
            now=_dt(body["now"]) if body.get("now") else None,
        )
        return 200, _receipt_view(receipt)

    return None


def _handle_get(service: EquityService, segments: list[str]):
    if segments == ["health"]:
        return 200, health_payload()
    if len(segments) == 3 and segments[:2] == ["api", "grants"]:
        return 200, _grant_view(service, service.get_grant(segments[2]))
    if len(segments) == 4 and segments[:2] == ["api", "employees"] and segments[3] == "statement":
        return 200, employee_statement(service, segments[2])
    if segments == ["api", "admin", "dashboard"]:
        return 200, admin_dashboard(service)
    return None


def create_server(host: str, port: int, service: EquityService | None = None) -> ThreadingHTTPServer:
    service = service or EquityService()

    class RequestHandler(BaseHTTPRequestHandler):
        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, handler, body=None) -> None:
            segments = [s for s in self.path.split("/") if s]
            try:
                result = handler(service, segments, body) if body is not None else handler(service, segments)
            except ServiceError as exc:
                self._respond(exc.status, exc.to_dict())
                return
            except (KeyError, ValueError) as exc:
                self._respond(400, {"error": {"code": "bad_request", "message": str(exc)}})
                return
            if result is None:
                self._respond(404, {"error": {"code": "not_found", "message": "路由不存在"}})
                return
            status, payload = result
            self._respond(status, payload)

        def do_GET(self) -> None:
            self._dispatch(_handle_get)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._respond(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
                return
            self._dispatch(_handle_post, body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), RequestHandler)
