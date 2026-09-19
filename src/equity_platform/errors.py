"""平台异常体系：所有业务拒绝都有稳定类型，便于 API 映射与审计留痕。"""
from __future__ import annotations


class PlatformError(Exception):
    """业务规则拒绝。"""


class WorkflowError(PlatformError):
    """当前流程状态不允许该操作。"""


class AmendmentError(PlatformError):
    """协议修订违反版本规则（追溯生效或倒改已结算/已归属批次）。"""


class ConflictError(PlatformError):
    """并发或幂等冲突：同一批次的重复结算、重复扣股或重复付款。"""


class MissingMarketDataError(PlatformError):
    """缺少市场价格、汇率或税务身份等结算必需资料。"""


class SettlementInputError(PlatformError):
    """结算输入不合法（股数、价格、汇率非正等）。"""
