"""证券过户与付款通道(模拟)。

真实系统对接券商/托管行与付款渠道;此处以可注入失败规则的模拟网关
代替,用于演示与测试:账户异常(如离职后证券账户已注销)时返回失败
回执,管理员可修正后重试。网关调用本身幂等:同一幂等键重复提交
返回同一回执号,不重复过户。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayAck:
    ok: bool
    confirmation_no: str | None = None
    failure_reason: str | None = None


class DepositoryGateway:
    def __init__(self, blocked_accounts: set[str] | None = None):
        self._blocked = set(blocked_accounts or ())
        self._processed: dict[str, GatewayAck] = {}

    def block_account(self, employee_id: str) -> None:
        self._blocked.add(employee_id)

    def unblock_account(self, employee_id: str) -> None:
        self._blocked.discard(employee_id)

    def transfer(
        self,
        *,
        employee_id: str,
        idempotency_key: str,
        shares: str,
        cash_items: list[dict],
    ) -> GatewayAck:
        if idempotency_key in self._processed:
            return self._processed[idempotency_key]
        if employee_id in self._blocked:
            ack = GatewayAck(ok=False, failure_reason="证券账户状态异常,过户被拒绝")
        else:
            confirmation = hashlib.sha256(
                f"{idempotency_key}:{employee_id}:{shares}".encode("utf-8")
            ).hexdigest()[:12].upper()
            ack = GatewayAck(ok=True, confirmation_no=f"CNF-{confirmation}")
        self._processed[idempotency_key] = ack
        return ack
