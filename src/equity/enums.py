"""状态机与业务枚举。"""

from __future__ import annotations

from enum import Enum


class TrancheState(str, Enum):
    """归属批次状态机。

    PENDING 待满足 -> LOCKED 锁定 -> EXERCISABLE 可执行 -> SETTLED 已结算
    任意非终态可因离职/未达标/窗口届满转入 LAPSED 失效;
    EXERCISABLE 遇黑窗期回退 LOCKED,黑窗结束后可再次进入。
    SETTLED 与 LAPSED 为终态,已结算批次不得倒改。
    """

    PENDING = "PENDING"
    LOCKED = "LOCKED"
    EXERCISABLE = "EXERCISABLE"
    SETTLED = "SETTLED"
    LAPSED = "LAPSED"


TERMINAL_STATES = (TrancheState.SETTLED, TrancheState.LAPSED)


class TerminationType(str, Enum):
    VOLUNTARY = "VOLUNTARY"              # 主动离职
    INVOLUNTARY = "INVOLUNTARY"          # 非自愿离职
    FOR_CAUSE = "FOR_CAUSE"              # 过失解除
    RETIREMENT = "RETIREMENT"            # 退休
    DEATH_DISABILITY = "DEATH_DISABILITY"  # 身故/伤残


class Role(str, Enum):
    """数据归属岗位:各自确认自己负责的事实。"""

    HR = "HR"                  # 人事:服务期间、休假、离职
    PAYROLL = "PAYROLL"        # 薪资:税务身份与预扣参数
    SECURITIES = "SECURITIES"  # 证券管理:协议版本、批次数量、价格与黑窗


class ReviewDecision(str, Enum):
    CONFIRMED = "CONFIRMED"  # 确认
    RETURNED = "RETURNED"    # 退回(必须附意见,留痕保留)


class SettlementMode(str, Enum):
    GROSS = "gross"                  # 全额交付,税由工资代扣
    NET_SHARE = "net_share"          # 净股交付:扣股抵税
    SELL_TO_COVER = "sell_to_cover"  # 卖股缴税


class ReceiptStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


from .codec import register as _register

for _cls in (
    TrancheState,
    TerminationType,
    Role,
    ReviewDecision,
    SettlementMode,
    ReceiptStatus,
):
    _register(_cls)
